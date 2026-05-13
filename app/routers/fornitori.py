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
from app.database import col_fornitori, col_invoices
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
    filtro = {}
    if cerca:
        filtro["$or"] = [
            {"ragione_sociale": {"$regex": cerca, "$options": "i"}},
            {"partita_iva":     {"$regex": cerca, "$options": "i"}},
        ]
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
