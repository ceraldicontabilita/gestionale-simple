"""
Router Fatture — Ceraldi Group ERP
CRUD fatture passive + import XML manuale + pagamento + scadenzario.
Collection: 'invoices' (nome esistente nel DB)
"""
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Query
from pydantic import BaseModel
from bson import ObjectId

from app.routers.auth import verify_token
from app.database import col_invoices, col_fornitori, col_scadenzario
from app.services.xml_parser import parse_file
from app.services.prima_nota_auto import smista_fattura

router = APIRouter(prefix="/api/fatture", tags=["fatture"])


def _serialize(doc: dict) -> dict:
    """Converte ObjectId e tipi non serializzabili in stringa."""
    if doc is None:
        return {}
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, list):
            out[k] = [_serialize(i) if isinstance(i, dict) else
                      str(i) if isinstance(i, ObjectId) else i
                      for i in v]
        elif isinstance(v, dict):
            out[k] = _serialize(v)
        else:
            out[k] = v
    return out


# ── GET /api/fatture ──────────────────────────────────────────────────────────
@router.get("")
async def lista_fatture(
    request: Request,
    anno:          Optional[int]  = Query(None),
    stato:         Optional[str]  = Query(None),   # da_pagare|pagata|annullata
    fornitore_piva: Optional[str] = Query(None),
    tipo_documento: Optional[str] = Query(None),   # TD01|TD04|ecc.
    source:        Optional[str]  = Query(None),   # pec|upload|manuale
    limit:         int            = Query(500),
    skip:          int            = Query(0),
    cerca:         Optional[str]  = Query(None),   # ricerca testuale
):
    verify_token(request)

    filtro = {}
    if anno:           filtro["anno"]          = anno
    if stato:          filtro["stato"]         = stato
    if fornitore_piva: filtro["fornitore_piva"] = fornitore_piva
    if tipo_documento: filtro["tipo_documento"] = tipo_documento
    if source:         filtro["source"]        = source
    if cerca:
        filtro["$or"] = [
            {"fornitore_nome":  {"$regex": cerca, "$options": "i"}},
            {"numero_fattura":  {"$regex": cerca, "$options": "i"}},
            {"fornitore_piva":  {"$regex": cerca, "$options": "i"}},
        ]

    cursor = col_invoices().find(filtro).sort("data_fattura", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    totale = await col_invoices().count_documents(filtro)

    # Arricchisci fornitore_nome dai fornitori per documenti con campo vuoto
    pive_missing = list({d["fornitore_piva"] for d in docs if not d.get("fornitore_nome") and d.get("fornitore_piva")})
    if pive_missing:
        forn_list = await col_fornitori().find(
            {"partita_iva": {"$in": pive_missing}},
            {"partita_iva": 1, "ragione_sociale": 1}
        ).to_list(length=1000)
        forn_map = {f["partita_iva"]: f.get("ragione_sociale", "") for f in forn_list}
        for d in docs:
            if not d.get("fornitore_nome") and d.get("fornitore_piva"):
                d["fornitore_nome"] = forn_map.get(d["fornitore_piva"], "")

    # KPI aggregati
    pipeline_kpi = [
        {"$match": filtro},
        {"$group": {
            "_id": None,
            "tot_imponibile": {"$sum": "$importo_imponibile"},
            "tot_iva":        {"$sum": "$importo_iva"},
            "tot_totale":     {"$sum": "$importo_totale"},
            "tot_iva_detr":   {"$sum": "$iva_detraibile"},
        }}
    ]
    kpi_cursor = col_invoices().aggregate(pipeline_kpi)
    kpi_list = await kpi_cursor.to_list(length=1)
    kpi = kpi_list[0] if kpi_list else {}

    return {
        "totale": totale,
        "fatture": [_serialize(d) for d in docs],
        "kpi": {
            "imponibile":   round(kpi.get("tot_imponibile", 0), 2),
            "iva":          round(kpi.get("tot_iva", 0), 2),
            "totale":       round(kpi.get("tot_totale", 0), 2),
            "iva_detr":     round(kpi.get("tot_iva_detr", 0), 2),
        }
    }


# ── GET /api/fatture/scadenzario ──────────────────────────────────────────────
@router.get("/scadenzario")
async def scadenzario(
    request: Request,
    giorni: int = Query(30),
):
    verify_token(request)
    from datetime import date, timedelta
    oggi = date.today().isoformat()
    limite = (date.today() + timedelta(days=giorni)).isoformat()

    filtro = {
        "stato": "da_pagare",
        "data_scadenza": {"$gte": oggi, "$lte": limite},
    }
    cursor = col_invoices().find(filtro).sort("data_scadenza", 1).limit(200)
    docs = await cursor.to_list(length=200)

    return {
        "scadenze": [_serialize(d) for d in docs],
        "totale": len(docs),
    }


# ── GET /api/fatture/{id} ─────────────────────────────────────────────────────
@router.get("/{fattura_id}")
async def dettaglio_fattura(request: Request, fattura_id: str):
    verify_token(request)
    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        # Prova anche con ObjectId
        try:
            doc = await col_invoices().find_one({"_id": ObjectId(fattura_id)})
        except Exception:
            pass
    if not doc:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return _serialize(doc)


# ── POST /api/fatture/import-xml ─────────────────────────────────────────────
@router.post("/import-xml")
async def import_xml(
    request: Request,
    file: UploadFile = File(...),
):
    """Upload manuale di un singolo file XML o P7M."""
    verify_token(request)

    if not file.filename.lower().endswith((".xml", ".p7m", ".xml.p7m")):
        raise HTTPException(status_code=400, detail="File deve essere .xml o .p7m")

    data = await file.read()

    try:
        fattura = parse_file(file.filename, data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Errore parsing: {e}")

    # Dedup per hash
    existing = await col_invoices().find_one({"xml_hash": fattura["xml_hash"]})
    if existing:
        return {
            "esito": "duplicata",
            "fattura_id": str(existing.get("_id", "")),
            "numero": fattura["numero_fattura"],
            "messaggio": "Fattura già presente nel sistema",
        }

    # Metodo pagamento: SEMPRE dall'anagrafica fornitore (l'XML viene ignorato)
    fornitore = await col_fornitori().find_one(
        {"partita_iva": fattura["fornitore_piva"]}
    )
    metodo = fornitore.get("metodo_pagamento", "") if fornitore else ""
    fattura["metodo_pagamento"] = metodo
    fattura["source"] = "upload"
    fattura["_id"] = str(uuid.uuid4())

    # Upsert fornitore
    if fattura["fornitore_piva"]:
        await col_fornitori().update_one(
            {"partita_iva": fattura["fornitore_piva"]},
            {
                "$setOnInsert": {
                    "partita_iva":     fattura["fornitore_piva"],
                    "metodo_pagamento": "",
                    "created_at":      datetime.utcnow().isoformat(),
                },
                "$set": {
                    "ragione_sociale": fattura["fornitore_nome"],
                    "updated_at":      datetime.utcnow().isoformat(),
                }
            },
            upsert=True,
        )

    await col_invoices().insert_one(fattura)
    fattura_id = fattura["_id"]

    # Smista in prima nota
    smistamento = await smista_fattura(fattura_id, fattura, metodo)

    return {
        "esito": "importata",
        "fattura_id": fattura_id,
        "numero": fattura["numero_fattura"],
        "fornitore": fattura["fornitore_nome"],
        "importo": fattura["importo_totale"],
        "prima_nota": smistamento,
    }


# ── POST /api/fatture/import-xml-bulk ────────────────────────────────────────
@router.post("/import-xml-bulk")
async def import_xml_bulk(
    request: Request,
    files: list[UploadFile] = File(...),
):
    """Upload di più file XML/P7M contemporaneamente."""
    verify_token(request)
    risultati = []
    for file in files:
        data = await file.read()
        try:
            fattura = parse_file(file.filename, data)
        except Exception as e:
            risultati.append({"file": file.filename, "esito": "errore", "messaggio": str(e)})
            continue

        existing = await col_invoices().find_one({"xml_hash": fattura["xml_hash"]})
        if existing:
            risultati.append({"file": file.filename, "esito": "duplicata",
                               "numero": fattura["numero_fattura"]})
            continue

        fornitore = await col_fornitori().find_one({"partita_iva": fattura["fornitore_piva"]})
        metodo = fornitore.get("metodo_pagamento", "") if fornitore else ""
        fattura["metodo_pagamento"] = metodo
        fattura["source"] = "upload"
        fattura["_id"] = str(uuid.uuid4())
        await col_invoices().insert_one(fattura)
        smistamento = await smista_fattura(fattura["_id"], fattura, metodo)
        risultati.append({
            "file": file.filename,
            "esito": "importata",
            "numero": fattura["numero_fattura"],
            "importo": fattura["importo_totale"],
            "prima_nota": smistamento,
        })

    nuove     = sum(1 for r in risultati if r["esito"] == "importata")
    duplicate = sum(1 for r in risultati if r["esito"] == "duplicata")
    errori    = sum(1 for r in risultati if r["esito"] == "errore")
    return {"nuove": nuove, "duplicate": duplicate, "errori": errori, "dettagli": risultati}


# ── POST /api/fatture/{id}/paga ───────────────────────────────────────────────
class PagaRequest(BaseModel):
    data_pagamento: str
    metodo: str      # MP01|MP02|MP08
    importo: Optional[float] = None
    note: Optional[str] = None


@router.post("/{fattura_id}/paga")
async def paga_fattura(request: Request, fattura_id: str, body: PagaRequest):
    """Registra un pagamento manuale per una fattura."""
    verify_token(request)

    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    if doc.get("stato") == "pagata":
        raise HTTPException(status_code=400, detail="Fattura già pagata")

    # Aggiorna metodo temporaneamente per smistamento
    doc["metodo_pagamento"] = body.metodo
    doc["data_fattura"]     = body.data_pagamento  # usa data pagamento come data movimento
    if body.importo:
        doc["importo_totale"] = body.importo

    smistamento = await smista_fattura(fattura_id, doc, body.metodo)

    await col_invoices().update_one(
        {"_id": fattura_id},
        {"$set": {
            "stato":           "pagata",
            "data_pagamento":  body.data_pagamento,
            "metodo_pagamento": body.metodo,
            "note_pagamento":  body.note or "",
        }}
    )

    return {"ok": True, "prima_nota": smistamento}


# ── PUT /api/fatture/{id} ─────────────────────────────────────────────────────
class AggiornFatturaRequest(BaseModel):
    metodo_pagamento:  Optional[str]   = None
    stato:             Optional[str]   = None
    data_scadenza:     Optional[str]   = None
    note:              Optional[str]   = None
    categoria_iva:     Optional[str]   = None
    detraibilita_pct:  Optional[float] = None


@router.put("/{fattura_id}")
async def aggiorna_fattura(request: Request, fattura_id: str, body: AggiornFatturaRequest):
    verify_token(request)
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nessun campo da aggiornare")
    update["updated_at"] = datetime.utcnow().isoformat()

    result = await col_invoices().update_one({"_id": fattura_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return {"ok": True}


# ── DELETE /api/fatture/{id} ──────────────────────────────────────────────────
@router.delete("/{fattura_id}")
async def elimina_fattura(request: Request, fattura_id: str):
    """Soft delete: marca la fattura come annullata e rimuove il movimento prima nota."""
    verify_token(request)
    from app.database import col_pn_cassa, col_pn_banca, col_pn_provvisori

    doc = await col_invoices().find_one({"_id": fattura_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Fattura non trovata")

    # Soft delete del movimento prima nota collegato
    prima_nota_id   = doc.get("prima_nota_id")
    prima_nota_tipo = doc.get("prima_nota_tipo")
    if prima_nota_id and prima_nota_tipo:
        col_map = {"cassa": col_pn_cassa(), "banca": col_pn_banca(),
                   "provvisorio": col_pn_provvisori()}
        col = col_map.get(prima_nota_tipo)
        if col:
            await col.update_one(
                {"_id": prima_nota_id},
                {"$set": {"status": "deleted", "deleted_at": datetime.utcnow().isoformat()}}
            )

    await col_invoices().update_one(
        {"_id": fattura_id},
        {"$set": {"stato": "annullata", "deleted_at": datetime.utcnow().isoformat()}}
    )
    return {"ok": True}


# ── POST /api/fatture/fix-data ────────────────────────────────────────────────
@router.post("/fix-data")
async def fix_data(request: Request):
    verify_token(request)
    from app.services.xml_parser import parse_fattura_xml
    fixed = 0
    errors = 0

    try:
        all_inv = await col_invoices().find({}).to_list(length=50000)
        piva_to_nome = {}

        for d in all_inv:
            raw_xml = d.get("raw_xml") or ""
            if not raw_xml:
                continue
            try:
                parsed = parse_fattura_xml(raw_xml.encode("utf-8", errors="replace"))
            except Exception:
                errors += 1
                continue

            update = {}
            if not (d.get("fornitore_nome") or ""):
                nome = parsed.get("fornitore_nome") or ""
                if nome:
                    update["fornitore_nome"] = nome
            if not (d.get("importo_imponibile") or 0):
                if parsed.get("importo_imponibile"):
                    update["importo_imponibile"] = parsed["importo_imponibile"]
                    update["importo_iva"] = parsed["importo_iva"]
                    update["iva_detraibile"] = parsed["iva_detraibile"]

            if update:
                await col_invoices().update_one({"_id": d["_id"]}, {"$set": update})
                fixed += 1

            piva = parsed.get("fornitore_piva") or d.get("fornitore_piva") or ""
            nome = update.get("fornitore_nome") or d.get("fornitore_nome") or ""
            if piva and nome:
                piva_to_nome[piva] = nome

        # Aggiorna ragione_sociale nei fornitori
        fixed_fornitori = 0
        for piva, nome in piva_to_nome.items():
            r = await col_fornitori().update_one(
                {"partita_iva": piva, "$or": [{"ragione_sociale": ""}, {"ragione_sociale": None}]},
                {"$set": {"ragione_sociale": nome}}
            )
            if r.modified_count:
                fixed_fornitori += 1

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore: {str(e)}")

    return {"ok": True, "fatture_fixate": fixed, "fornitori_fixati": fixed_fornitori, "errori": errors}
