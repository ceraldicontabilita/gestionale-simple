"""
Router Verbali/Multe — Ceraldi Group ERP
Upload PDF verbale → parse AI → scadenzario → pagamento → prima nota uscita cassa/banca.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import get_db
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/verbali", tags=["verbali"])


def col_v():
    return get_db()["verbali_noleggio"]


# ── POST /api/verbali/upload ──────────────────────────────────────────────────
@router.post("/upload")
async def upload_verbale(
    request: Request,
    file:    UploadFile = File(...),
):
    """Carica PDF verbale/multa → Claude estrae dati → salva con scadenza."""
    verify_token(request)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF accettati")

    pdf_bytes = await file.read()
    from app.services.ai_parser import parse_verbale

    try:
        dati = await parse_verbale(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Errore parsing PDF: {e}")

    if "errore" in dati:
        raise HTTPException(status_code=422, detail=dati["errore"])

    # Dedup per numero verbale
    numero = dati.get("numero_verbale", "")
    if numero:
        existing = await col_v().find_one({"numero_verbale": numero})
        if existing:
            return {"skip": True, "id": str(existing["_id"]), "motivo": "verbale già presente"}

    doc = {
        "_id":               new_id(),
        "numero_verbale":    numero,
        "targa":             dati.get("targa", "").upper(),
        "data_verbale":      dati.get("data_verbale", ""),
        "importo_originale": dati.get("importo_originale", 0.0),
        "importo_ridotto":   dati.get("importo_ridotto", 0.0),
        "importo_da_pagare": dati.get("importo_ridotto", 0.0) or dati.get("importo_originale", 0.0),
        "scadenza_pagamento": dati.get("scadenza_pagamento", ""),
        "ente_emittente":    dati.get("ente_emittente", ""),
        "articolo_violato":  dati.get("articolo_violato", ""),
        "luogo_infrazione":  dati.get("luogo_infrazione", ""),
        "iuv":               dati.get("iuv", ""),
        "cbill":             dati.get("cbill", ""),
        "stato":             "da_pagare",
        "source":            "upload_pdf",
        "filename":          file.filename,
        "note":              dati.get("note", ""),
        "created_at":        datetime.utcnow().isoformat(),
    }
    await col_v().insert_one(doc)
    return {"ok": True, "id": doc["_id"], "dati": dati}


# ── POST /api/verbali (inserimento manuale) ───────────────────────────────────
class NuovoVerbale(BaseModel):
    numero_verbale:    str
    targa:             str
    data_verbale:      str
    importo_originale: float
    importo_ridotto:   Optional[float] = None
    scadenza_pagamento: Optional[str]  = None
    ente_emittente:    Optional[str]   = None
    articolo_violato:  Optional[str]   = None
    iuv:               Optional[str]   = None
    note:              Optional[str]   = None


@router.post("")
async def crea_verbale(request: Request, body: NuovoVerbale):
    verify_token(request)
    existing = await col_v().find_one({"numero_verbale": body.numero_verbale})
    if existing:
        raise HTTPException(status_code=409, detail="Verbale già presente")

    doc = {
        "_id":               new_id(),
        "numero_verbale":    body.numero_verbale,
        "targa":             body.targa.upper(),
        "data_verbale":      body.data_verbale,
        "importo_originale": body.importo_originale,
        "importo_ridotto":   body.importo_ridotto or body.importo_originale,
        "importo_da_pagare": body.importo_ridotto or body.importo_originale,
        "scadenza_pagamento": body.scadenza_pagamento or "",
        "ente_emittente":    body.ente_emittente or "",
        "articolo_violato":  body.articolo_violato or "",
        "iuv":               body.iuv or "",
        "stato":             "da_pagare",
        "source":            "manuale",
        "note":              body.note or "",
        "created_at":        datetime.utcnow().isoformat(),
    }
    await col_v().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── GET /api/verbali ──────────────────────────────────────────────────────────
@router.get("")
async def lista_verbali(
    request: Request,
    stato:   Optional[str] = Query(None),   # da_pagare | pagato | ricorso
    targa:   Optional[str] = Query(None),
    anno:    Optional[int] = Query(None),
    limit:   int           = Query(200),
):
    verify_token(request)
    filtro: dict = {}
    if stato:
        filtro["stato"] = stato
    if targa:
        filtro["targa"] = targa.upper()
    if anno:
        filtro["data_verbale"] = {"$regex": f"^{anno}"}

    cursor = col_v().find(filtro).sort("scadenza_pagamento", 1).limit(limit)
    docs   = await cursor.to_list(length=limit)

    totale_da_pagare = sum(
        d.get("importo_da_pagare", 0.0)
        for d in docs if d.get("stato") == "da_pagare"
    )
    return {
        "verbali": [_ser(d) for d in docs],
        "totale":  len(docs),
        "totale_da_pagare": round(totale_da_pagare, 2),
    }


# ── POST /api/verbali/{id}/paga ───────────────────────────────────────────────
class PagaVerbale(BaseModel):
    data_pagamento: str
    importo:        Optional[float] = None
    metodo:         str = "MP01"   # MP01=cassa, MP08=assegno, altro=banca
    note:           Optional[str]  = None


@router.post("/{verbale_id}/paga")
async def paga_verbale(request: Request, verbale_id: str, body: PagaVerbale):
    """Registra il pagamento e crea movimento in prima nota."""
    verify_token(request)
    db      = get_db()
    verbale = await col_v().find_one({"_id": verbale_id})
    if not verbale:
        raise HTTPException(status_code=404, detail="Verbale non trovato")
    if verbale.get("stato") == "pagato":
        raise HTTPException(status_code=409, detail="Verbale già pagato")

    importo = body.importo or verbale.get("importo_da_pagare", 0.0)
    targa   = verbale.get("targa", "")
    numero  = verbale.get("numero_verbale", verbale_id)

    # Prima nota
    mov = {
        "_id":         new_id(),
        "tipo":        "uscita",
        "importo":     importo,
        "data":        body.data_pagamento,
        "descrizione": f"Multa {numero} — {targa} — {verbale.get('ente_emittente','')}",
        "categoria":   "verbale_multa",
        "verbale_id":  verbale_id,
        "note":        body.note or "",
        "created_at":  datetime.utcnow().isoformat(),
    }

    metodo = body.metodo.upper()
    if metodo in ("MP01",):
        await db["prima_nota_cassa"].insert_one(mov)
        destinazione = "cassa"
    elif metodo in ("MP08",):
        await db["prima_nota_assegni"].insert_one(mov)
        destinazione = "assegni"
    else:
        await db["prima_nota_banca"].insert_one(mov)
        destinazione = "banca"

    await col_v().update_one(
        {"_id": verbale_id},
        {"$set": {
            "stato":           "pagato",
            "data_pagamento":  body.data_pagamento,
            "importo_pagato":  importo,
            "prima_nota_id":   mov["_id"],
            "prima_nota_dest": destinazione,
            "updated_at":      datetime.utcnow().isoformat(),
        }}
    )
    return {"ok": True, "prima_nota_id": mov["_id"], "destinazione": destinazione}


# ── PUT /api/verbali/{id}/ricorso ─────────────────────────────────────────────
@router.put("/{verbale_id}/ricorso")
async def segna_ricorso(request: Request, verbale_id: str, note: str = ""):
    verify_token(request)
    r = await col_v().update_one(
        {"_id": verbale_id},
        {"$set": {"stato": "ricorso", "note_ricorso": note, "updated_at": datetime.utcnow().isoformat()}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Verbale non trovato")
    return {"ok": True}


# ── DELETE /api/verbali/{id} ──────────────────────────────────────────────────
@router.delete("/{verbale_id}")
async def elimina_verbale(request: Request, verbale_id: str):
    verify_token(request)
    r = await col_v().update_one(
        {"_id": verbale_id},
        {"$set": {"stato": "eliminato", "deleted_at": datetime.utcnow().isoformat()}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Verbale non trovato")
    return {"ok": True}
