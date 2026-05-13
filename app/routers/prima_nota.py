"""
Router Prima Nota — Ceraldi Group ERP
Gestisce: Cassa, Banca, Provvisori, Sposta, Conferma.
"""
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import col_pn_cassa, col_pn_banca, col_pn_provvisori
from app.services.prima_nota_auto import conferma_provvisorio, sposta_movimento

router = APIRouter(prefix="/api/prima-nota", tags=["prima-nota"])


def _serialize(doc):
    if doc is None:
        return {}
    from bson import ObjectId
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _nuovi_id() -> str:
    return str(uuid.uuid4())


async def _saldo_collection(col, filtro: dict) -> dict:
    pipeline = [
        {"$match": {**filtro, "status": {"$ne": "deleted"}}},
        {"$group": {
            "_id": "$tipo",
            "totale": {"$sum": "$importo"}
        }}
    ]
    cursor = col.aggregate(pipeline)
    rows = await cursor.to_list(length=10)
    entrate = 0.0
    uscite  = 0.0
    for r in rows:
        if r["_id"] == "entrata":
            entrate = r["totale"]
        elif r["_id"] == "uscita":
            uscite = r["totale"]
    return {
        "entrate": round(entrate, 2),
        "uscite":  round(uscite, 2),
        "saldo":   round(entrate - uscite, 2),
    }


# ── GET cassa ─────────────────────────────────────────────────────────────────
@router.get("/cassa")
async def get_cassa(
    request: Request,
    anno:  Optional[int] = Query(None),
    mese:  Optional[int] = Query(None),
    limit: int = Query(500),
):
    verify_token(request)
    filtro: dict = {"status": {"$ne": "deleted"}}
    if anno: filtro["anno"] = anno
    if mese:
        pfx = f"{anno or ''}-{str(mese).zfill(2)}"
        filtro["data"] = {"$regex": f"^{pfx}"}

    cursor = col_pn_cassa().find(filtro).sort("data", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)
    totale = await col_pn_cassa().count_documents(filtro)
    saldo  = await _saldo_collection(col_pn_cassa(), {
        k: v for k, v in filtro.items() if k != "status"
    })

    return {
        "movimenti": [_serialize(d) for d in docs],
        "totale":    totale,
        **saldo,
    }


# ── GET banca ─────────────────────────────────────────────────────────────────
@router.get("/banca")
async def get_banca(
    request: Request,
    anno:          Optional[int]  = Query(None),
    mese:          Optional[int]  = Query(None),
    riconciliato:  Optional[bool] = Query(None),
    limit:         int            = Query(500),
):
    verify_token(request)
    filtro: dict = {"status": {"$ne": "deleted"}}
    if anno:                      filtro["anno"] = anno
    if mese:
        pfx = f"{anno or ''}-{str(mese).zfill(2)}"
        filtro["data"] = {"$regex": f"^{pfx}"}
    if riconciliato is not None:  filtro["riconciliato"] = riconciliato

    cursor = col_pn_banca().find(filtro).sort("data", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)
    totale = await col_pn_banca().count_documents(filtro)
    saldo  = await _saldo_collection(col_pn_banca(), {
        k: v for k, v in filtro.items() if k not in ("status", "riconciliato")
    })

    return {
        "movimenti": [_serialize(d) for d in docs],
        "totale":    totale,
        **saldo,
    }


# ── GET provvisori ────────────────────────────────────────────────────────────
@router.get("/provvisori")
async def get_provvisori(
    request: Request,
    anno:  Optional[int] = Query(None),
    stato: str = Query("pending"),   # pending | confermato
    limit: int = Query(200),
):
    verify_token(request)
    filtro: dict = {"stato": stato}
    if anno: filtro["anno"] = anno

    cursor = col_pn_provvisori().find(filtro).sort("created_at", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)
    totale = await col_pn_provvisori().count_documents(filtro)

    tot_importo = sum(d.get("importo", 0) for d in docs)

    return {
        "provvisori":    [_serialize(d) for d in docs],
        "totale":        totale,
        "totale_importo": round(tot_importo, 2),
    }


# ── POST cassa — movimento manuale ────────────────────────────────────────────
class MovimentoManuale(BaseModel):
    data:        str
    tipo:        str          # entrata | uscita
    importo:     float
    descrizione: str
    categoria:   Optional[str] = "Spese generali"
    note:        Optional[str] = None
    fattura_id:  Optional[str] = None
    fornitore_piva: Optional[str] = None


@router.post("/cassa")
async def crea_cassa(request: Request, body: MovimentoManuale):
    verify_token(request)
    if body.tipo not in ("entrata", "uscita"):
        raise HTTPException(status_code=400, detail="tipo deve essere 'entrata' o 'uscita'")

    anno = 0
    if body.data and len(body.data) >= 4:
        try: anno = int(body.data[:4])
        except Exception: pass

    doc = {
        "_id":          _nuovi_id(),
        "data":         body.data,
        "tipo":         body.tipo,
        "importo":      abs(body.importo),
        "descrizione":  body.descrizione,
        "categoria":    body.categoria or "Spese generali",
        "note":         body.note or "",
        "fattura_id":   body.fattura_id,
        "fornitore_piva": body.fornitore_piva,
        "source":       "manuale",
        "anno":         anno,
        "status":       "active",
        "created_at":   datetime.utcnow().isoformat(),
    }
    await col_pn_cassa().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


@router.post("/banca")
async def crea_banca(request: Request, body: MovimentoManuale):
    verify_token(request)
    if body.tipo not in ("entrata", "uscita"):
        raise HTTPException(status_code=400, detail="tipo deve essere 'entrata' o 'uscita'")

    anno = 0
    if body.data and len(body.data) >= 4:
        try: anno = int(body.data[:4])
        except Exception: pass

    doc = {
        "_id":          _nuovi_id(),
        "data":         body.data,
        "tipo":         body.tipo,
        "importo":      abs(body.importo),
        "descrizione":  body.descrizione,
        "categoria":    body.categoria or "Spese generali",
        "note":         body.note or "",
        "fattura_id":   body.fattura_id,
        "fornitore_piva": body.fornitore_piva,
        "is_assegno":   False,
        "riconciliato": False,
        "source":       "manuale",
        "anno":         anno,
        "status":       "active",
        "created_at":   datetime.utcnow().isoformat(),
    }
    await col_pn_banca().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── POST /provvisori/{id}/conferma ────────────────────────────────────────────
class ConfermaRequest(BaseModel):
    destinazione:           str     # cassa | banca
    data_pagamento:         Optional[str] = None
    salva_metodo_fornitore: bool = False


@router.post("/provvisori/{prov_id}/conferma")
async def conferma_prov(request: Request, prov_id: str, body: ConfermaRequest):
    verify_token(request)
    if body.destinazione not in ("cassa", "banca"):
        raise HTTPException(status_code=400, detail="destinazione deve essere 'cassa' o 'banca'")
    try:
        result = await conferma_provvisorio(
            prov_id,
            body.destinazione,
            body.data_pagamento or "",
            body.salva_metodo_fornitore,
        )
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── POST /sposta ──────────────────────────────────────────────────────────────
class SpostaRequest(BaseModel):
    movimento_id: str
    da:           str   # cassa | banca
    a:            str   # cassa | banca


@router.post("/sposta")
async def sposta(request: Request, body: SpostaRequest):
    verify_token(request)
    if body.da == body.a:
        raise HTTPException(status_code=400, detail="Sorgente e destinazione uguali")
    VALID = ("cassa", "banca", "provvisori")
    if body.da not in VALID or body.a not in VALID:
        raise HTTPException(status_code=400, detail="Valori da/a non validi")
    try:
        result = await sposta_movimento(body.movimento_id, body.da, body.a)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── DELETE soft ───────────────────────────────────────────────────────────────
@router.delete("/cassa/{mov_id}")
async def elimina_cassa(request: Request, mov_id: str):
    verify_token(request)
    r = await col_pn_cassa().update_one(
        {"_id": mov_id},
        {"$set": {"status": "deleted", "deleted_at": datetime.utcnow().isoformat()}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Movimento non trovato")
    return {"ok": True}


@router.delete("/banca/{mov_id}")
async def elimina_banca(request: Request, mov_id: str):
    verify_token(request)
    r = await col_pn_banca().update_one(
        {"_id": mov_id},
        {"$set": {"status": "deleted", "deleted_at": datetime.utcnow().isoformat()}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Movimento non trovato")
    return {"ok": True}


# ── GET riepilogo mensile (per commercialista) ────────────────────────────────
@router.get("/riepilogo/{anno}/{mese}")
async def riepilogo_mensile(request: Request, anno: int, mese: int):
    """Riepilogo cassa + banca per un mese specifico — export per commercialista."""
    verify_token(request)
    pfx = f"{anno}-{str(mese).zfill(2)}"
    filtro_mese = {"data": {"$regex": f"^{pfx}"}, "status": {"$ne": "deleted"}}

    cassa_docs = await col_pn_cassa().find(filtro_mese).sort("data", 1).to_list(length=1000)
    banca_docs = await col_pn_banca().find(filtro_mese).sort("data", 1).to_list(length=1000)

    def agg(docs):
        entrate = sum(d["importo"] for d in docs if d.get("tipo") == "entrata")
        uscite  = sum(d["importo"] for d in docs if d.get("tipo") == "uscita")
        return round(entrate, 2), round(uscite, 2), round(entrate - uscite, 2)

    ce, cu, cs = agg(cassa_docs)
    be, bu, bs = agg(banca_docs)

    return {
        "anno": anno, "mese": mese,
        "cassa": {"entrate": ce, "uscite": cu, "saldo": cs,
                  "movimenti": [_serialize(d) for d in cassa_docs]},
        "banca": {"entrate": be, "uscite": bu, "saldo": bs,
                  "movimenti": [_serialize(d) for d in banca_docs]},
    }
