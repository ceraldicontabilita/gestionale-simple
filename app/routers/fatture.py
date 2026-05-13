"""
Router Fatture Ricevute — Ceraldi Group ERP
Ciclo passivo completo: import XML/P7M, dedup, fornitore, prima nota, scadenziario.

Regole fondamentali (da spec operativa):
 1. Metodo pagamento SEMPRE dall'anagrafica fornitore — mai dall'XML
 2. Dedup su: hash file AND (piva + numero + data)
 3. Fornitore cercato per piva, poi cf; se assente → creato automaticamente
 4. TD04/TD08 (note credito) → importi negativi
 5. Metodo non definito → stato "da_classificare" + alert
 6. Nessuna scrittura DB nelle chiamate GET
"""
from datetime import datetime, date, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from bson import ObjectId

from app.routers.auth import verify_token
from app.database import (
    col_invoices, col_fornitori,
    col_pn_cassa, col_pn_banca, col_pn_provvisori,
)
from app.services.xml_parser import parse_file
from app.services.prima_nota_auto import smista_fattura
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/fatture", tags=["fatture"])


# ── Fornitore: trova o crea ───────────────────────────────────────────────────

async def _trova_o_crea_fornitore(fattura: dict) -> tuple[dict, bool]:
    """
    Cerca il fornitore per P.IVA poi C.F.
    Se non trovato lo crea con dati minimi e flag alert_incompleto=True.
    Ritorna (fornitore_doc, creato_adesso).
    """
    piva = (fattura.get("fornitore_piva") or "").strip()
    cf   = (fattura.get("fornitore_cf")   or "").strip()
    nome = (fattura.get("fornitore_nome") or "").strip()

    forn = None
    if piva:
        forn = await col_fornitori().find_one({"partita_iva": piva})
    if not forn and cf:
        forn = await col_fornitori().find_one({"codice_fiscale": cf})

    if forn:
        # Arricchisce ragione_sociale se mancante
        if nome and not (forn.get("ragione_sociale") or "").strip():
            await col_fornitori().update_one(
                {"_id": forn["_id"]},
                {"$set": {"ragione_sociale": nome,
                           "updated_at": datetime.utcnow().isoformat()}}
            )
            forn["ragione_sociale"] = nome
        return forn, False

    # Non creare un fornitore senza almeno P.IVA o ragione_sociale
    if not piva and not cf and not nome:
        return {}, False

    # Crea con dati minimi dall'XML
    nuovo: dict = {
        "_id":              new_id(),
        "partita_iva":      piva,
        "codice_fiscale":   cf,
        "ragione_sociale":  nome,
        "indirizzo":        fattura.get("fornitore_indirizzo", ""),
        "cap":              fattura.get("fornitore_cap", ""),
        "citta":            fattura.get("fornitore_citta", ""),
        "iban":             fattura.get("fornitore_iban", ""),
        "pec":              "",
        "email":            "",
        "metodo_pagamento": "",
        "categoria_iva":    "generico",
        "detraibilita_default_pct": 100.0,
        "alert_incompleto": True,
        "source":           "auto_da_fattura",
        "attivo":           True,
        "created_at":       datetime.utcnow().isoformat(),
        "updated_at":       datetime.utcnow().isoformat(),
    }
    await col_fornitori().insert_one(nuovo)
    return nuovo, True


# ── Deduplicazione ────────────────────────────────────────────────────────────

async def _is_duplicata(fattura: dict) -> str | None:
    """Controlla duplicati. Ritorna motivo se duplicata, None se nuova."""
    if await col_invoices().find_one({"xml_hash": fattura["xml_hash"]}):
        return "hash"
    piva = fattura.get("fornitore_piva", "")
    num  = fattura.get("numero_fattura", "")
    data = fattura.get("data_fattura", "")
    if piva and num and data:
        if await col_invoices().find_one({
            "fornitore_piva": piva,
            "numero_fattura": num,
            "data_fattura":   data,
        }):
            return "chiave_fiscale"
    return None


# ── GET /api/fatture ──────────────────────────────────────────────────────────

@router.get("")
async def lista_fatture(
    request:        Request,
    anno:           Optional[int] = Query(None),
    stato:          Optional[str] = Query(None),
    fornitore_piva: Optional[str] = Query(None),
    tipo_documento: Optional[str] = Query(None),
    cerca:          Optional[str] = Query(None),
    limit:          int           = Query(200),
    skip:           int           = Query(0),
):
    verify_token(request)

    filtro: dict = {"deleted_at": {"$exists": False}}
    if anno:           filtro["anno"]           = anno
    if stato:          filtro["stato"]          = stato
    if fornitore_piva: filtro["fornitore_piva"] = fornitore_piva
    if tipo_documento: filtro["tipo_documento"] = tipo_documento
    if cerca:
        filtro["$or"] = [
            {"fornitore_nome": {"$regex": cerca, "$options": "i"}},
            {"numero_fattura": {"$regex": cerca, "$options": "i"}},
            {"fornitore_piva": {"$regex": cerca, "$options": "i"}},
        ]

    cursor = col_invoices().find(filtro).sort("data_fattura", -1).skip(skip).limit(limit)
    docs   = await cursor.to_list(length=limit)
    totale = await col_invoices().count_documents(filtro)

    # Mappa piva→nome dai fornitori (solo lettura, nessuna scrittura)
    all_forn = await col_fornitori().find(
        {}, {"partita_iva": 1, "ragione_sociale": 1}
    ).to_list(length=None)
    forn_map = {
        f["partita_iva"]: (f.get("ragione_sociale") or "")
        for f in all_forn if f.get("partita_iva")
    }

    risultato = []
    for d in docs:
        if not d.get("fornitore_nome"):
            d["fornitore_nome"] = forn_map.get(d.get("fornitore_piva", ""), "")
        risultato.append(_ser(d))

    # KPI aggregati
    kpi_pipe = [{"$match": filtro}, {"$group": {
        "_id":        None,
        "imponibile": {"$sum": "$importo_imponibile"},
        "iva":        {"$sum": "$importo_iva"},
        "totale":     {"$sum": "$importo_totale"},
        "iva_detr":   {"$sum": "$iva_detraibile"},
    }}]
    kpi_list = await col_invoices().aggregate(kpi_pipe).to_list(length=1)
    kpi = kpi_list[0] if kpi_list else {}

    return {
        "totale": totale,
        "fatture": risultato,
        "kpi": {
            "imponibile": round(kpi.get("imponibile", 0), 2),
            "iva":        round(kpi.get("iva", 0), 2),
            "totale":     round(kpi.get("totale", 0), 2),
            "iva_detr":   round(kpi.get("iva_detr", 0), 2),
        },
    }


# ── GET /api/fatture/scadenzario ──────────────────────────────────────────────

@router.get("/scadenzario")
async def scadenzario_fatture(request: Request, giorni: int = Query(30)):
    verify_token(request)
    oggi   = date.today().isoformat()
    limite = (date.today() + timedelta(days=giorni)).isoformat()
    cursor = col_invoices().find({
        "stato":         {"$in": ["da_pagare", "scaduta"]},
        "data_scadenza": {"$gte": oggi, "$lte": limite},
        "deleted_at":    {"$exists": False},
    }).sort("data_scadenza", 1).limit(200)
    docs = await cursor.to_list(length=200)
    return {"scadenze": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/fatture/{id} ─────────────────────────────────────────────────────

@router.get("/{fattura_id}")
async def dettaglio_fattura(request: Request, fattura_id: str):
    verify_token(request)
    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        try:
            doc = await col_invoices().find_one({"_id": ObjectId(fattura_id)})
        except Exception:
            pass
    if not doc:
        raise HTTPException(404, "Fattura non trovata")
    return _ser(doc)


# ── POST /api/fatture/import-xml ─────────────────────────────────────────────

@router.post("/import-xml")
async def import_xml(request: Request, file: UploadFile = File(...)):
    """Importa un singolo file XML o P7M FatturaPA."""
    verify_token(request)

    if not file.filename.lower().endswith((".xml", ".p7m", ".xml.p7m")):
        raise HTTPException(400, "File deve essere .xml o .p7m")

    raw = await file.read()
    try:
        fattura = parse_file(file.filename, raw)
    except Exception as e:
        raise HTTPException(422, f"Errore parsing XML: {e}")

    # Controllo duplicati
    motivo_dup = await _is_duplicata(fattura)
    if motivo_dup:
        return {
            "esito":     "duplicata",
            "motivo":    motivo_dup,
            "numero":    fattura["numero_fattura"],
            "fornitore": fattura["fornitore_nome"],
            "messaggio": "Documento già presente nel sistema",
        }

    # Fornitore
    forn, creato = await _trova_o_crea_fornitore(fattura)

    # Metodo pagamento: SEMPRE dall'anagrafica fornitore, mai dall'XML
    metodo = (forn.get("metodo_pagamento") or "").strip() if forn else ""
    fattura["metodo_pagamento"] = metodo

    # Nota credito TD04/TD08: importi negativi
    if fattura.get("tipo_documento") in ("TD04", "TD08"):
        for campo in ("importo_imponibile", "importo_iva", "importo_totale", "iva_detraibile"):
            fattura[campo] = -abs(fattura.get(campo) or 0)

    # Stato iniziale
    fattura["stato"]                  = "da_pagare" if metodo else "da_classificare"
    fattura["source"]                 = "upload"
    fattura["_id"]                    = new_id()
    fattura["alert_fornitore_nuovo"]  = creato
    fattura["alert_metodo_mancante"]  = not bool(metodo)

    await col_invoices().insert_one(fattura)

    # Prima nota automatica
    smist = await smista_fattura(fattura["_id"], fattura, metodo)

    return {
        "esito":                "importata",
        "fattura_id":           fattura["_id"],
        "numero":               fattura["numero_fattura"],
        "data":                 fattura["data_fattura"],
        "tipo_documento":       fattura["tipo_documento"],
        "fornitore":            fattura["fornitore_nome"],
        "fornitore_piva":       fattura["fornitore_piva"],
        "importo_imponibile":   fattura["importo_imponibile"],
        "importo_iva":          fattura["importo_iva"],
        "importo_totale":       fattura["importo_totale"],
        "metodo_pagamento":     metodo or "",
        "prima_nota":           smist,
        "alert_fornitore_nuovo": creato,
        "alert_metodo_mancante": not bool(metodo),
    }


# ── POST /api/fatture/import-xml-bulk ────────────────────────────────────────

@router.post("/import-xml-bulk")
async def import_xml_bulk(request: Request, files: list[UploadFile] = File(...)):
    """Importa più file XML/P7M in un'unica chiamata."""
    verify_token(request)
    risultati = []

    for file in files:
        raw = await file.read()
        try:
            fattura = parse_file(file.filename, raw)
        except Exception as e:
            risultati.append({
                "file": file.filename, "esito": "errore", "messaggio": str(e)
            })
            continue

        motivo_dup = await _is_duplicata(fattura)
        if motivo_dup:
            risultati.append({
                "file": file.filename, "esito": "duplicata",
                "numero": fattura["numero_fattura"], "motivo": motivo_dup,
            })
            continue

        forn, creato = await _trova_o_crea_fornitore(fattura)
        metodo = (forn.get("metodo_pagamento") or "").strip() if forn else ""
        fattura["metodo_pagamento"] = metodo

        if fattura.get("tipo_documento") in ("TD04", "TD08"):
            for campo in ("importo_imponibile", "importo_iva", "importo_totale", "iva_detraibile"):
                fattura[campo] = -abs(fattura.get(campo) or 0)

        fattura["stato"]                 = "da_pagare" if metodo else "da_classificare"
        fattura["source"]                = "upload"
        fattura["_id"]                   = new_id()
        fattura["alert_fornitore_nuovo"] = creato
        fattura["alert_metodo_mancante"] = not bool(metodo)

        await col_invoices().insert_one(fattura)
        smist = await smista_fattura(fattura["_id"], fattura, metodo)

        risultati.append({
            "file":           file.filename,
            "esito":          "importata",
            "numero":         fattura["numero_fattura"],
            "fornitore":      fattura["fornitore_nome"],
            "importo_totale": fattura["importo_totale"],
            "prima_nota":     smist,
            "alert_fornitore_nuovo": creato,
            "alert_metodo_mancante": not bool(metodo),
        })

    nuove     = sum(1 for r in risultati if r["esito"] == "importata")
    duplicate = sum(1 for r in risultati if r["esito"] == "duplicata")
    errori    = sum(1 for r in risultati if r["esito"] == "errore")
    return {
        "nuove": nuove, "duplicate": duplicate, "errori": errori,
        "dettagli": risultati,
    }


# ── POST /api/fatture/{id}/paga ───────────────────────────────────────────────

class PagaRequest(BaseModel):
    data_pagamento: str
    metodo: str
    importo: Optional[float] = None
    note: Optional[str] = None


@router.post("/{fattura_id}/paga")
async def paga_fattura(request: Request, fattura_id: str, body: PagaRequest):
    verify_token(request)
    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        raise HTTPException(404, "Fattura non trovata")
    if doc.get("stato") == "pagata":
        raise HTTPException(400, "Fattura già pagata")

    doc_tmp = dict(doc)
    doc_tmp["metodo_pagamento"] = body.metodo
    doc_tmp["data_fattura"]     = body.data_pagamento
    if body.importo:
        doc_tmp["importo_totale"] = body.importo

    smist = await smista_fattura(fattura_id, doc_tmp, body.metodo)

    await col_invoices().update_one(
        {"_id": fattura_id},
        {"$set": {
            "stato":            "pagata",
            "data_pagamento":   body.data_pagamento,
            "metodo_pagamento": body.metodo,
            "note_pagamento":   body.note or "",
        }}
    )
    return {"ok": True, "prima_nota": smist}


# ── PUT /api/fatture/{id} ─────────────────────────────────────────────────────

class AggiornFatturaRequest(BaseModel):
    metodo_pagamento: Optional[str]   = None
    stato:            Optional[str]   = None
    data_scadenza:    Optional[str]   = None
    note:             Optional[str]   = None
    categoria_iva:    Optional[str]   = None
    detraibilita_pct: Optional[float] = None


@router.put("/{fattura_id}")
async def aggiorna_fattura(
    request: Request, fattura_id: str, body: AggiornFatturaRequest
):
    verify_token(request)
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(400, "Nessun campo da aggiornare")
    update["updated_at"] = datetime.utcnow().isoformat()
    result = await col_invoices().update_one({"_id": fattura_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(404, "Fattura non trovata")
    return {"ok": True}


# ── DELETE /api/fatture/{id} ──────────────────────────────────────────────────

@router.delete("/{fattura_id}")
async def elimina_fattura(request: Request, fattura_id: str):
    """Soft delete: annulla la fattura e il movimento prima nota collegato."""
    verify_token(request)
    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        raise HTTPException(404, "Fattura non trovata")

    prima_nota_id   = doc.get("prima_nota_id")
    prima_nota_tipo = doc.get("prima_nota_tipo")
    if prima_nota_id and prima_nota_tipo:
        col_map = {
            "cassa":       col_pn_cassa(),
            "banca":       col_pn_banca(),
            "provvisorio": col_pn_provvisori(),
        }
        col = col_map.get(prima_nota_tipo)
        if col:
            await col.update_one(
                {"_id": prima_nota_id},
                {"$set": {"status": "deleted",
                           "deleted_at": datetime.utcnow().isoformat()}}
            )

    await col_invoices().update_one(
        {"_id": fattura_id},
        {"$set": {"stato": "annullata",
                   "deleted_at": datetime.utcnow().isoformat()}}
    )
    return {"ok": True}


# ── POST /api/fatture/migrazione-nomi ─────────────────────────────────────────

@router.post("/migrazione-nomi")
async def migrazione_nomi(request: Request):
    """
    One-time migration: popola fornitore_nome sui documenti che ce l'hanno vuoto
    usando la ragione_sociale dall'anagrafica fornitori (match per partita_iva).
    Sicuro da richiamare più volte (idempotente).
    """
    verify_token(request)

    # Carica tutti i fornitori con partita_iva e ragione_sociale
    all_forn = await col_fornitori().find(
        {"ragione_sociale": {"$exists": True, "$ne": ""}},
        {"partita_iva": 1, "ragione_sociale": 1}
    ).to_list(length=None)
    forn_map = {
        f["partita_iva"]: f["ragione_sociale"]
        for f in all_forn if f.get("partita_iva") and f.get("ragione_sociale")
    }

    if not forn_map:
        return {"aggiornate": 0, "messaggio": "Nessun fornitore con ragione_sociale trovato"}

    # Trova fatture senza fornitore_nome
    cursor = col_invoices().find(
        {"$or": [{"fornitore_nome": ""}, {"fornitore_nome": None},
                 {"fornitore_nome": {"$exists": False}}]},
        {"_id": 1, "fornitore_piva": 1}
    )
    docs = await cursor.to_list(length=None)

    aggiornate = 0
    for d in docs:
        piva = d.get("fornitore_piva", "")
        nome = forn_map.get(piva, "")
        if nome:
            await col_invoices().update_one(
                {"_id": d["_id"]},
                {"$set": {"fornitore_nome": nome}}
            )
            aggiornate += 1

    return {
        "aggiornate": aggiornate,
        "fatture_senza_nome": len(docs),
        "fornitori_disponibili": len(forn_map),
    }
