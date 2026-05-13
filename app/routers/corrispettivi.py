"""
Router Corrispettivi — Ceraldi Group ERP

Gestisce:
  1. Import XML dal registratore telematico
  2. Smistamento automatico in Prima Nota Cassa:
       - ENTRATA cassa = totale corrispettivo giornaliero
       - USCITA  cassa = quota POS (che passa in banca il giorno dopo)
  3. Confronto POS a 3 vie:
       - Fonte A: XML registratore (automatico)
       - Fonte B: dichiarazione manuale serale operatore
       - Fonte C: estratto conto bancario (accredito POS giorno successivo)
     → Se A ≠ B ≠ C → ALERT discrepanza (evita sanzioni AdE)
  4. KPI ricavi per bilancio (RICAVI = solo corrispettivi)
"""
import uuid
from datetime import datetime, date, timedelta
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Query
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import (
    col_corrispettivi, col_pn_cassa, col_pn_banca, col_estratto_conto
)
from app.services.corrispettivi_parser import parse_corrispettivo_xml

router = APIRouter(prefix="/api/corrispettivi", tags=["corrispettivi"])

# Soglia in euro per considerare una discrepanza "accettabile"
# (il POS accredita il giorno successivo → differenza fisiologica)
SOGLIA_DISCREPANZA = 5.0


def _ser(doc):
    if not doc:
        return {}
    from bson import ObjectId
    return {k: str(v) if isinstance(v, ObjectId) else v for k, v in doc.items()}


def _nuovo_id() -> str:
    return str(uuid.uuid4())


# ── POST /api/corrispettivi/import-xml ────────────────────────────────────────
@router.post("/import-xml")
async def import_xml(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Importa un file XML del registratore telematico.
    Crea automaticamente:
      - ENTRATA in prima_nota_cassa (totale giornaliero)
      - USCITA in prima_nota_cassa (quota POS → transita in banca)
    """
    verify_token(request)

    if not file.filename.lower().endswith(".xml"):
        raise HTTPException(status_code=400, detail="File deve essere .xml")

    data = await file.read()

    try:
        corr = parse_corrispettivo_xml(data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Dedup per hash XML
    existing = await col_corrispettivi().find_one({"xml_hash": corr["xml_hash"]})
    if existing:
        return {
            "esito": "duplicato",
            "id": str(existing.get("_id", "")),
            "data": corr["data"],
            "messaggio": "Corrispettivo già importato",
        }

    # Dedup per data (un solo corrispettivo per giorno)
    existing_data = await col_corrispettivi().find_one({"data": corr["data"]})
    if existing_data:
        return {
            "esito": "duplicato",
            "id": str(existing_data.get("_id", "")),
            "data": corr["data"],
            "messaggio": f"Corrispettivo per il {corr['data']} già presente",
        }

    corr["_id"] = _nuovo_id()
    corr["raw_xml"] = data.decode("utf-8", errors="replace")

    # ── Smistamento Prima Nota Cassa ──────────────────────────────────────────
    pn_ids = await _crea_movimenti_prima_nota(corr)
    corr["prima_nota_cassa_id"]    = pn_ids["entrata_id"]
    corr["prima_nota_uscita_pos_id"] = pn_ids["uscita_pos_id"]

    await col_corrispettivi().insert_one(corr)

    return {
        "esito":   "importato",
        "id":      corr["_id"],
        "data":    corr["data"],
        "totale":  corr["totale"],
        "contanti": corr["contanti"],
        "elettronico": corr["elettronico"],
        "prima_nota": pn_ids,
    }


async def _crea_movimenti_prima_nota(corr: dict) -> dict:
    """
    Per ogni corrispettivo crea 2 movimenti in prima_nota_cassa:
      1. ENTRATA: totale incassato (contanti + POS)
      2. USCITA: quota POS (che fisicamente va in banca il giorno dopo)
    Il saldo cassa risultante = solo i contanti effettivi rimasti in cassa.
    """
    anno = corr.get("anno", 0)
    data = corr.get("data", "")
    totale      = corr.get("totale", 0.0)
    elettronico = corr.get("elettronico", 0.0)
    desc_base   = f"Corrispettivi {data}"

    entrata_id    = None
    uscita_pos_id = None

    # 1. ENTRATA = totale giornaliero
    if totale > 0:
        entrata_doc = {
            "_id":          _nuovo_id(),
            "data":         data,
            "tipo":         "entrata",
            "importo":      totale,
            "descrizione":  f"{desc_base} — Incasso totale",
            "categoria":    "Corrispettivi",
            "source":       "corrispettivo_xml",
            "corrispettivo_id": corr["_id"],
            "anno":         anno,
            "status":       "active",
            "created_at":   datetime.utcnow().isoformat(),
        }
        await col_pn_cassa().insert_one(entrata_doc)
        entrata_id = entrata_doc["_id"]

    # 2. USCITA POS = quota elettronica (va in banca)
    if elettronico > 0:
        uscita_doc = {
            "_id":          _nuovo_id(),
            "data":         data,
            "tipo":         "uscita",
            "importo":      elettronico,
            "descrizione":  f"{desc_base} — Quota POS → Banca",
            "categoria":    "Girofondi POS",
            "source":       "corrispettivo_xml",
            "corrispettivo_id": corr["_id"],
            "anno":         anno,
            "status":       "active",
            "created_at":   datetime.utcnow().isoformat(),
        }
        await col_pn_cassa().insert_one(uscita_doc)
        uscita_pos_id = uscita_doc["_id"]

    return {"entrata_id": entrata_id, "uscita_pos_id": uscita_pos_id}


# ── GET /api/corrispettivi ────────────────────────────────────────────────────
@router.get("")
async def lista_corrispettivi(
    request: Request,
    anno:   Optional[int] = Query(None),
    mese:   Optional[int] = Query(None),
    limit:  int           = Query(365),
):
    verify_token(request)
    filtro = {}
    if anno: filtro["anno"] = anno
    if mese:
        pfx = f"{anno or ''}-{str(mese).zfill(2)}"
        filtro["data"] = {"$regex": f"^{pfx}"}

    cursor = col_corrispettivi().find(filtro, {"raw_xml": 0}).sort("data", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)

    # KPI aggregati
    tot_ricavi     = sum(d.get("totale", 0) for d in docs)
    tot_iva        = sum(d.get("totale_iva", 0) for d in docs)
    tot_imponibile = sum(d.get("totale_imponibile", 0) for d in docs)
    tot_contanti   = sum(d.get("contanti", 0) for d in docs)
    tot_pos        = sum(d.get("elettronico", 0) for d in docs)
    discrepanze    = [d for d in docs if d.get("discrepanza") and abs(d["discrepanza"]) > SOGLIA_DISCREPANZA]

    return {
        "corrispettivi": [_ser(d) for d in docs],
        "totale_giorni": len(docs),
        "kpi": {
            "ricavi":       round(tot_ricavi, 2),
            "imponibile":   round(tot_imponibile, 2),
            "iva_debito":   round(tot_iva, 2),
            "contanti":     round(tot_contanti, 2),
            "pos":          round(tot_pos, 2),
            "discrepanze":  len(discrepanze),
        },
    }


# ── GET /api/corrispettivi/{id} ───────────────────────────────────────────────
@router.get("/{corr_id}")
async def dettaglio_corrispettivo(request: Request, corr_id: str):
    verify_token(request)
    doc = await col_corrispettivi().find_one({"_id": corr_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Corrispettivo non trovato")
    return _ser(doc)


# ── POST /api/corrispettivi manuale ──────────────────────────────────────────
class CorrispettivoManuale(BaseModel):
    data:            str
    totale:          float
    contanti:        float
    elettronico:     float
    totale_iva:      Optional[float] = None
    totale_imponibile: Optional[float] = None
    note:            Optional[str] = None


@router.post("/manuale")
async def crea_manuale(request: Request, body: CorrispettivoManuale):
    """Inserimento manuale di corrispettivi (quando non si ha il file XML)."""
    verify_token(request)

    # Dedup per data
    existing = await col_corrispettivi().find_one({"data": body.data})
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Corrispettivo per il {body.data} già presente"
        )

    anno = int(body.data[:4]) if len(body.data) >= 4 else 0
    iva = body.totale_iva if body.totale_iva is not None else round(body.totale / 11, 2)
    imponibile = body.totale_imponibile if body.totale_imponibile is not None else round(body.totale - iva, 2)

    doc = {
        "_id":             _nuovo_id(),
        "xml_hash":        None,
        "data":            body.data,
        "anno":            anno,
        "totale":          body.totale,
        "totale_imponibile": imponibile,
        "totale_iva":      iva,
        "contanti":        body.contanti,
        "elettronico":     body.elettronico,
        "altri":           round(body.totale - body.contanti - body.elettronico, 2),
        "aliquote_iva":    [],
        "pos_dichiarato":  body.elettronico,
        "pos_banca":       None,
        "discrepanza":     None,
        "note":            body.note or "",
        "source":          "manuale",
        "created_at":      datetime.utcnow().isoformat(),
    }
    pn_ids = await _crea_movimenti_prima_nota(doc)
    doc["prima_nota_cassa_id"]      = pn_ids["entrata_id"]
    doc["prima_nota_uscita_pos_id"] = pn_ids["uscita_pos_id"]

    await col_corrispettivi().insert_one(doc)
    return {"esito": "creato", "id": doc["_id"], "prima_nota": pn_ids}


# ── POST /api/corrispettivi/{id}/pos-dichiarato ───────────────────────────────
class PosDichiarato(BaseModel):
    importo: float    # quanto l'operatore dichiara di aver incassato via POS


@router.post("/{corr_id}/pos-dichiarato")
async def aggiorna_pos_dichiarato(request: Request, corr_id: str, body: PosDichiarato):
    """
    Fonte B: l'operatore dichiara manualmente la quota POS incassata a fine giornata.
    Aggiorna il confronto e calcola eventuali discrepanze.
    """
    verify_token(request)
    doc = await col_corrispettivi().find_one({"_id": corr_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Corrispettivo non trovato")

    pos_xml   = doc.get("elettronico", 0.0)    # Fonte A
    pos_dich  = body.importo                    # Fonte B
    pos_banca = doc.get("pos_banca")            # Fonte C (se già trovata)

    discrepanza_ab = round(pos_dich - pos_xml, 2)
    discrepanza_bc = round((pos_banca - pos_dich), 2) if pos_banca is not None else None

    alert = abs(discrepanza_ab) > SOGLIA_DISCREPANZA

    await col_corrispettivi().update_one(
        {"_id": corr_id},
        {"$set": {
            "pos_dichiarato": pos_dich,
            "discrepanza":    discrepanza_ab,
            "discrepanza_bc": discrepanza_bc,
            "alert_discrepanza": alert,
            "updated_at":     datetime.utcnow().isoformat(),
        }}
    )

    return {
        "ok":          True,
        "pos_xml":     pos_xml,
        "pos_dich":    pos_dich,
        "pos_banca":   pos_banca,
        "discrepanza": discrepanza_ab,
        "alert":       alert,
    }


# ── POST /api/corrispettivi/confronto-pos ────────────────────────────────────
@router.post("/confronto-pos")
async def confronto_pos_automatico(
    request: Request,
    anno:   Optional[int] = Query(None),
    mese:   Optional[int] = Query(None),
):
    """
    Fonte C — Cerca automaticamente nell'estratto conto bancario
    gli accrediti POS (giorno successivo) e li abbina ai corrispettivi.

    Logica:
    - Per ogni corrispettivo con elettronico > 0
    - Cerca in estratto_conto_movimenti un accredito nel giorno +1 o +2
    - Descrizione contiene "POS" o "CARTE" o "SumUp" o "Nexi" o "Worldline"
    - Importo simile (±5€ per arrotondamenti commissioni)
    """
    verify_token(request)

    filtro: dict = {}
    if anno: filtro["anno"] = anno
    if mese:
        pfx = f"{anno or ''}-{str(mese).zfill(2)}"
        filtro["data"] = {"$regex": f"^{pfx}"}

    corr_docs = await col_corrispettivi().find(
        {**filtro, "elettronico": {"$gt": 0}, "pos_banca": None}
    ).to_list(length=365)

    risultati = []
    abbinati  = 0
    non_trovati = 0

    KEYWORDS_POS = ["pos", "carte", "sumup", "nexi", "worldline",
                    "sella", "gestpay", "cartasi", "mastercard", "visa",
                    "accredito carte", "pagamenti elettronici"]

    for corr in corr_docs:
        data_corr = corr["data"]
        pos_atteso = corr.get("elettronico", 0.0)

        # Cerca accredito nei 2 giorni successivi
        try:
            d = date.fromisoformat(data_corr)
        except Exception:
            continue

        trovato = None
        for delta in [1, 2, 0]:  # +1 giorno (normale), +2 (weekend), stesso giorno
            data_cerca = (d + timedelta(days=delta)).isoformat()
            # Cerca in estratto conto: entrata + importo simile + keyword POS
            cursor = col_estratto_conto().find({
                "data": data_cerca,
                "tipo": "entrata",
                "importo": {
                    "$gte": pos_atteso - 10,
                    "$lte": pos_atteso + 10,
                }
            })
            movimenti_giorno = await cursor.to_list(length=20)

            for mov in movimenti_giorno:
                desc = (mov.get("descrizione", "") or "").lower()
                if any(kw in desc for kw in KEYWORDS_POS):
                    trovato = mov
                    break
            if trovato:
                break

        if trovato:
            pos_banca     = trovato.get("importo", 0.0)
            discrepanza   = round(pos_banca - pos_atteso, 2)
            alert         = abs(discrepanza) > SOGLIA_DISCREPANZA
            abbinati += 1

            await col_corrispettivi().update_one(
                {"_id": corr["_id"]},
                {"$set": {
                    "pos_banca":         pos_banca,
                    "estratto_conto_id": str(trovato.get("_id", "")),
                    "discrepanza_bc":    discrepanza,
                    "alert_discrepanza": alert,
                    "updated_at":        datetime.utcnow().isoformat(),
                }}
            )
            risultati.append({
                "data":        data_corr,
                "pos_xml":     pos_atteso,
                "pos_banca":   pos_banca,
                "discrepanza": discrepanza,
                "alert":       alert,
                "esito":       "abbinato",
            })
        else:
            non_trovati += 1
            risultati.append({
                "data":    data_corr,
                "pos_xml": pos_atteso,
                "esito":   "non_trovato",
            })

    return {
        "abbinati":    abbinati,
        "non_trovati": non_trovati,
        "risultati":   risultati,
    }


# ── GET /api/corrispettivi/discrepanze ────────────────────────────────────────
@router.get("/discrepanze/lista")
async def lista_discrepanze(
    request: Request,
    anno:     Optional[int] = Query(None),
    solo_alert: bool        = Query(True),
):
    """
    Mostra tutti i giorni con discrepanze POS.
    Critico per evitare sanzioni Agenzia delle Entrate.
    """
    verify_token(request)
    filtro: dict = {}
    if anno: filtro["anno"] = anno
    if solo_alert:
        filtro["alert_discrepanza"] = True
    else:
        filtro["discrepanza"] = {"$ne": None}

    cursor = col_corrispettivi().find(filtro, {"raw_xml": 0}).sort("data", -1)
    docs = await cursor.to_list(length=200)

    totale_discrepanza = sum(
        abs(d.get("discrepanza", 0) or 0) for d in docs
    )

    return {
        "discrepanze":         [_ser(d) for d in docs],
        "totale":              len(docs),
        "totale_discrepanza":  round(totale_discrepanza, 2),
        "soglia_accettabile":  SOGLIA_DISCREPANZA,
    }


# ── GET /api/corrispettivi/riepilogo/bilancio ─────────────────────────────────
@router.get("/riepilogo/bilancio")
async def riepilogo_bilancio(
    request: Request,
    anno: int = Query(...),
):
    """
    Riepilogo annuale corrispettivi per bilancio e liquidazione IVA.
    RICAVI = totale corrispettivi (non fatture emesse).
    IVA DEBITO = totale_iva corrispettivi.
    """
    verify_token(request)

    pipeline = [
        {"$match": {"anno": anno}},
        {"$group": {
            "_id": {"$substr": ["$data", 5, 2]},   # mese (MM)
            "ricavi":     {"$sum": "$totale"},
            "imponibile": {"$sum": "$totale_imponibile"},
            "iva_debito": {"$sum": "$totale_iva"},
            "contanti":   {"$sum": "$contanti"},
            "pos":        {"$sum": "$elettronico"},
            "giorni":     {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    cursor = col_corrispettivi().aggregate(pipeline)
    mesi = await cursor.to_list(length=12)

    totali = {
        "ricavi":     round(sum(m["ricavi"]     for m in mesi), 2),
        "imponibile": round(sum(m["imponibile"] for m in mesi), 2),
        "iva_debito": round(sum(m["iva_debito"] for m in mesi), 2),
        "contanti":   round(sum(m["contanti"]   for m in mesi), 2),
        "pos":        round(sum(m["pos"]        for m in mesi), 2),
        "giorni":     sum(m["giorni"] for m in mesi),
    }

    return {
        "anno":   anno,
        "mesi":   [{
            "mese":       m["_id"],
            "ricavi":     round(m["ricavi"], 2),
            "imponibile": round(m["imponibile"], 2),
            "iva_debito": round(m["iva_debito"], 2),
            "contanti":   round(m["contanti"], 2),
            "pos":        round(m["pos"], 2),
            "giorni":     m["giorni"],
        } for m in mesi],
        "totali": totali,
    }


# ── DELETE /api/corrispettivi/{id} ────────────────────────────────────────────
@router.delete("/{corr_id}")
async def elimina_corrispettivo(request: Request, corr_id: str):
    """
    Elimina un corrispettivo e i suoi movimenti prima nota collegati.
    ATTENZIONE: operazione irreversibile.
    """
    verify_token(request)
    doc = await col_corrispettivi().find_one({"_id": corr_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Corrispettivo non trovato")

    # Soft delete movimenti prima nota collegati
    for campo in ["prima_nota_cassa_id", "prima_nota_uscita_pos_id"]:
        pid = doc.get(campo)
        if pid:
            await col_pn_cassa().update_one(
                {"_id": pid},
                {"$set": {"status": "deleted", "deleted_at": datetime.utcnow().isoformat()}}
            )

    await col_corrispettivi().delete_one({"_id": corr_id})
    return {"ok": True}
