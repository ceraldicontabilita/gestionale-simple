"""
Router PEC — Ceraldi Group ERP
Sync Aruba IMAP + processa singola email.
"""
from typing import Optional
from fastapi import APIRouter, Request, Query
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.services.pec_service import sync_pec, processa_singola_pec
from app.database import col_pec_inbox

router = APIRouter(prefix="/api/pec", tags=["pec"])


class SyncRequest(BaseModel):
    giorni_indietro:   int  = 7
    forza_riprocessa:  bool = False


@router.post("/sync")
async def pec_sync(request: Request, body: SyncRequest = SyncRequest()):
    """Scarica email non lette da Aruba PEC e processa le fatture."""
    verify_token(request)
    result = await sync_pec(
        giorni_indietro=body.giorni_indietro,
        forza_riprocessa=body.forza_riprocessa,
    )
    return result


@router.post("/repair-fornitori")
async def repair_fornitori(request: Request):
    """Riscarica 60 giorni di PEC per ripristinare fornitore_nome mancante sulle fatture importate senza quel campo."""
    verify_token(request)
    result = await sync_pec(giorni_indietro=60, forza_riprocessa=True)
    return result


@router.post("/processa/{pec_id}")
async def processa_pec(request: Request, pec_id: str):
    """Riprocessa una singola email PEC già in inbox."""
    verify_token(request)
    result = await processa_singola_pec(pec_id)
    return result


@router.get("/inbox")
async def pec_inbox(
    request: Request,
    processato: Optional[bool] = Query(None),
    limit:      int            = Query(100),
):
    """Lista email PEC ricevute (raw inbox)."""
    verify_token(request)
    filtro = {}
    if processato is not None:
        filtro["processato"] = processato

    cursor = col_pec_inbox().find(filtro, {"raw_xml": 0}).sort("data_ricezione", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        from bson import ObjectId
        if isinstance(d.get("_id"), ObjectId):
            d["_id"] = str(d["_id"])
    return {"inbox": docs, "totale": len(docs)}
