"""
Router Fornitori — Ceraldi Group ERP
Anagrafica fornitori + metodo pagamento + lookup AI categoria IVA.
"""
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
import httpx

from app.routers.auth import verify_token
from app.database import col_fornitori, col_invoices, get_db
from app.services.ai_parser import classifica_fornitore_da_piva
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/fornitori", tags=["fornitori"])

OPENAPI_TOKEN = os.getenv("OPENAPI_TOKEN", "")


# ── GET /api/fornitori ────────────────────────────────────────────────────────
@router.get("")
async def lista_fornitori(
    request: Request,
    cerca:   Optional[str] = Query(None),
    metodo:  Optional[str] = Query(None),
    limit:   int           = Query(300),
):
    verify_token(request)
    # Escludi documenti orphan senza né ragione_sociale né partita_iva
    filtro: dict = {"$or": [
        {"ragione_sociale": {"$nin": [None, ""]}},
        {"partita_iva":     {"$nin": [None, ""]}},
    ]}
    if cerca:
        filtro["$and"] = [{"$or": filtro.pop("$or")}, {"$or": [
            {"ragione_sociale": {"$regex": cerca, "$options": "i"}},
            {"partita_iva":     {"$regex": cerca, "$options": "i"}},
        ]}]
    if metodo:
        filtro["metodo_pagamento"] = metodo

    from pymongo import ASCENDING
    cursor = col_fornitori().find(filtro).collation({"locale": "it", "strength": 2}).sort("ragione_sociale", ASCENDING).limit(limit)
    docs = await cursor.to_list(length=limit)

    # Arricchisci con totale fatturato e conteggio anno corrente
    anno = datetime.now().year
    pive = [d.get("partita_iva", "") for d in docs if d.get("partita_iva")]
    if pive:
        pipeline = [
            {"$match": {"fornitore_piva": {"$in": pive}, "anno": anno}},
            {"$group": {"_id": "$fornitore_piva",
                        "totale": {"$sum": "$importo_totale"},
                        "count":  {"$sum": 1}}},
        ]
        agg = await col_invoices().aggregate(pipeline).to_list(length=5000)
        agg_map = {r["_id"]: r for r in agg}
    else:
        agg_map = {}

    for d in docs:
        piva = d.get("partita_iva", "")
        info = agg_map.get(piva, {})
        d["fatture_anno"] = info.get("count", 0)
        d["totale_fatturato"] = info.get("totale", 0.0)

    return {"fornitori": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/fornitori/{piva} ─────────────────────────────────────────────────
@router.get("/{piva}")
async def dettaglio_fornitore(request: Request, piva: str):
    verify_token(request)
    doc = await col_fornitori().find_one({"partita_iva": piva})
    if not doc:
        raise HTTPException(status_code=404, detail="Fornitore non trovato")

    # Ultime 10 fatture
    cursor = col_invoices().find({"fornitore_piva": piva}).sort("data_fattura", -1).limit(10)
    fatture = await cursor.to_list(length=10)

    return {
        "fornitore": _ser(doc),
        "ultime_fatture": [_ser(f) for f in fatture],
    }


# ── POST /api/fornitori ───────────────────────────────────────────────────────
class NuovoFornitore(BaseModel):
    partita_iva:     str
    ragione_sociale: str
    codice_fiscale:  Optional[str] = None
    metodo_pagamento: Optional[str] = None   # MP01|MP02|MP08
    iban:            Optional[str] = None
    email:           Optional[str] = None
    indirizzo:       Optional[str] = None
    categoria_iva:   Optional[str] = None
    detraibilita_default_pct: Optional[float] = None


@router.post("")
async def crea_fornitore(request: Request, body: NuovoFornitore):
    verify_token(request)
    existing = await col_fornitori().find_one({"partita_iva": body.partita_iva})
    if existing:
        raise HTTPException(status_code=409, detail="Fornitore già presente")

    doc = {
        "_id":            new_id(),
        "partita_iva":    body.partita_iva,
        "ragione_sociale": body.ragione_sociale,
        "codice_fiscale": body.codice_fiscale or "",
        "metodo_pagamento": body.metodo_pagamento or "",
        "iban":           body.iban or "",
        "email":          body.email or "",
        "indirizzo":      body.indirizzo or "",
        "categoria_iva":  body.categoria_iva or "",
        "detraibilita_default_pct": body.detraibilita_default_pct or 100.0,
        "ateco_code":     "",
        "created_at":     datetime.utcnow().isoformat(),
    }
    await col_fornitori().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── PUT /api/fornitori/{piva} ─────────────────────────────────────────────────
class AggiornaFornitore(BaseModel):
    ragione_sociale:  Optional[str]   = None
    metodo_pagamento: Optional[str]   = None
    iban:             Optional[str]   = None
    email:            Optional[str]   = None
    indirizzo:        Optional[str]   = None
    categoria_iva:    Optional[str]   = None
    detraibilita_default_pct: Optional[float] = None
    ateco_code:       Optional[str]   = None
    note:             Optional[str]   = None


@router.put("/{piva}")
async def aggiorna_fornitore(request: Request, piva: str, body: AggiornaFornitore):
    verify_token(request)
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nessun campo da aggiornare")
    update["updated_at"] = datetime.utcnow().isoformat()

    # Se cambio metodo pagamento → salva storico
    if "metodo_pagamento" in update:
        await col_fornitori().update_one(
            {"partita_iva": piva},
            {"$push": {"storico_metodo": {
                "metodo": update["metodo_pagamento"],
                "data":   datetime.utcnow().isoformat(),
            }}}
        )

    r = await col_fornitori().update_one({"partita_iva": piva}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Fornitore non trovato")
    return {"ok": True}


# ── POST /api/fornitori/{piva}/classifica-iva ─────────────────────────────────
@router.post("/{piva}/classifica-iva")
async def classifica_iva(request: Request, piva: str):
    """Usa Claude per classificare la categoria IVA del fornitore e salvarla."""
    verify_token(request)
    doc = await col_fornitori().find_one({"partita_iva": piva})
    if not doc:
        raise HTTPException(status_code=404, detail="Fornitore non trovato")

    nome = doc.get("ragione_sociale", "")
    result = await classifica_fornitore_da_piva(piva, nome)

    # Salva classificazione nel DB
    await col_fornitori().update_one(
        {"partita_iva": piva},
        {"$set": {
            "categoria_iva":           result.get("categoria_iva", "generico"),
            "detraibilita_default_pct": result.get("detraibilita_pct", 100.0),
            "ateco_code":              result.get("ateco_suggerito", ""),
            "classificazione_ai":      result,
            "classificato_at":         datetime.utcnow().isoformat(),
        }}
    )
    return {"ok": True, "classificazione": result}


# ── GET /api/fornitori/stats ─────────────────────────────────────────────────
@router.get("/stats")
async def fornitori_stats(request: Request):
    verify_token(request)
    anno = datetime.now().year
    totale = await col_fornitori().count_documents({
        "$or": [{"ragione_sociale": {"$nin": [None, ""]}}, {"partita_iva": {"$nin": [None, ""]}}]
    })
    pipeline = [
        {"$match": {"anno": anno}},
        {"$group": {"_id": "$fornitore_piva", "totale": {"$sum": "$importo_totale"}, "count": {"$sum": 1}}},
        {"$sort": {"totale": -1}},
        {"$limit": 10},
    ]
    top_forn = await col_invoices().aggregate(pipeline).to_list(10)
    return {
        "totale_fornitori": totale,
        "anno":             anno,
        "top_fornitori":    top_forn,
    }


# ── GET /api/fornitori/{piva}/fatture ─────────────────────────────────────────
@router.get("/{piva}/fatture")
async def fatture_fornitore(
    request: Request,
    piva:    str,
    anno:    Optional[int] = Query(None),
    limit:   int           = Query(50),
):
    verify_token(request)
    filtro: dict = {"fornitore_piva": piva}
    if anno:
        filtro["anno"] = anno
    cursor = col_invoices().find(filtro).sort("data_fattura", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return {"fatture": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/fornitori/{piva}/fatturato ───────────────────────────────────────
@router.get("/{piva}/fatturato")
async def fatturato_fornitore(request: Request, piva: str):
    verify_token(request)
    pipeline = [
        {"$match": {"fornitore_piva": piva}},
        {"$group": {
            "_id":   "$anno",
            "totale_imponibile": {"$sum": "$importo_imponibile"},
            "totale_iva":        {"$sum": "$importo_iva"},
            "totale_fatture":    {"$sum": "$importo_totale"},
            "count":             {"$sum": 1},
        }},
        {"$sort": {"_id": -1}},
    ]
    rows = await col_invoices().aggregate(pipeline).to_list(10)
    return {"per_anno": rows}


# ── GET /api/fornitori/{piva}/iban-from-fatture ───────────────────────────────
@router.get("/{piva}/iban-from-fatture")
async def iban_from_fatture(request: Request, piva: str):
    """Estrae l'IBAN dalle ultime fatture XML e lo salva nell'anagrafica."""
    verify_token(request)
    cursor = col_invoices().find(
        {"fornitore_piva": piva, "fornitore_iban": {"$nin": [None, ""]}},
    ).sort("data_fattura", -1).limit(5)
    docs = await cursor.to_list(5)
    if not docs:
        return {"ok": False, "iban": None, "msg": "Nessun IBAN trovato nelle fatture"}
    iban = docs[0].get("fornitore_iban", "")
    if iban:
        await col_fornitori().update_one(
            {"partita_iva": piva},
            {"$set": {"iban": iban, "updated_at": datetime.utcnow().isoformat()}},
        )
    return {"ok": bool(iban), "iban": iban}


# ── GET /api/fornitori/{piva}/dati-da-fatture ─────────────────────────────────
@router.get("/{piva}/dati-da-fatture")
async def dati_da_fatture(request: Request, piva: str):
    """Aggiorna anagrafica fornitore usando i dati estratti dall'ultima fattura XML."""
    verify_token(request)
    last = await col_invoices().find_one(
        {"fornitore_piva": piva},
        sort=[("data_fattura", -1)],
    )
    if not last:
        return {"ok": False, "msg": "Nessuna fattura trovata"}

    update: dict = {"updated_at": datetime.utcnow().isoformat()}
    mapping = {
        "fornitore_nome":      "ragione_sociale",
        "fornitore_cf":        "codice_fiscale",
        "fornitore_indirizzo": "indirizzo",
        "fornitore_cap":       "cap",
        "fornitore_citta":     "citta",
        "fornitore_iban":      "iban",
    }
    for src, dst in mapping.items():
        val = last.get(src, "")
        if val:
            update[dst] = val

    if update:
        await col_fornitori().update_one(
            {"partita_iva": piva},
            {"$set": update},
            upsert=False,
        )
    return {"ok": True, "aggiornati": list(update.keys())}


# ── GET /api/fornitori/{piva}/scadenze ────────────────────────────────────────
@router.get("/{piva}/scadenze")
async def scadenze_fornitore(request: Request, piva: str):
    verify_token(request)
    db = get_db()
    docs = await db["scadenziario_fornitori"].find(
        {"fornitore_piva": piva, "pagato": {"$ne": True}}
    ).sort("data_scadenza", 1).to_list(100)
    return {"scadenze": [_ser(d) for d in docs]}


# ── GET /api/fornitori/{piva}/lookup-cciaa ────────────────────────────────────
@router.get("/{piva}/lookup-cciaa")
async def lookup_cciaa(request: Request, piva: str):
    """Cerca dati Camera di Commercio via OpenAPI."""
    verify_token(request)
    if not OPENAPI_TOKEN:
        raise HTTPException(status_code=503, detail="OPENAPI_TOKEN non configurato")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.openapi.it/cciaa/it/{piva}",
                headers={"x-api-key": OPENAPI_TOKEN},
            )
        if resp.status_code == 200:
            data = resp.json()
            # Aggiorna dati nel DB
            update = {}
            if data.get("denominazione"):
                update["ragione_sociale"] = data["denominazione"]
            if data.get("ateco"):
                update["ateco_code"] = data["ateco"]
            if data.get("indirizzo"):
                update["indirizzo"] = data["indirizzo"]
            if update:
                update["updated_at"] = datetime.utcnow().isoformat()
                await col_fornitori().update_one(
                    {"partita_iva": piva}, {"$set": update}
                )
            return {"ok": True, "dati": data}
        else:
            return {"ok": False, "status": resp.status_code}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Errore lookup: {e}")
