"""
Router Dipendenti — Ceraldi Group ERP
CRUD anagrafica + presenze + import bulk da Zucchetti / impresasemplice.
"""
import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import get_db

router = APIRouter(prefix="/api/dipendenti", tags=["dipendenti"])


def _ser(doc):
    if not doc:
        return {}
    from bson import ObjectId
    return {k: str(v) if isinstance(v, ObjectId) else v for k, v in doc.items()}


def col_dip():
    return get_db()["dipendenti"]


def col_pres():
    return get_db()["presenze"]


# ── GET /api/dipendenti ───────────────────────────────────────────────────────
@router.get("")
async def lista_dipendenti(
    request: Request,
    attivo:  Optional[bool] = Query(None),
    cerca:   Optional[str]  = Query(None),
):
    verify_token(request)
    filtro = {}
    if attivo is not None:
        filtro["attivo"] = attivo
    if cerca:
        filtro["$or"] = [
            {"nome":            {"$regex": cerca, "$options": "i"}},
            {"cognome":         {"$regex": cerca, "$options": "i"}},
            {"codice_fiscale":  {"$regex": cerca, "$options": "i"}},
        ]

    cursor = col_dip().find(filtro).sort("cognome", 1)
    docs   = await cursor.to_list(length=300)
    return {"dipendenti": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/dipendenti/{cf} ──────────────────────────────────────────────────
@router.get("/{cf}")
async def dettaglio_dipendente(request: Request, cf: str):
    verify_token(request)
    doc = await col_dip().find_one({"codice_fiscale": cf.upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")

    # Ultimi 12 cedolini
    db     = get_db()
    cursor = db["cedolini"].find({"codice_fiscale": cf.upper()}).sort("mese_riferimento", -1).limit(12)
    cedolini = await cursor.to_list(length=12)

    return {
        "dipendente": _ser(doc),
        "cedolini":   [_ser(c) for c in cedolini],
    }


# ── POST /api/dipendenti ──────────────────────────────────────────────────────
class NuovoDipendente(BaseModel):
    codice_fiscale:  str
    nome:            str
    cognome:         str
    qualifica:       Optional[str]  = None
    livello:         Optional[str]  = None
    iban:            Optional[str]  = None
    email:           Optional[str]  = None
    telefono:        Optional[str]  = None
    data_assunzione: Optional[str]  = None
    ccnl:            Optional[str]  = None   # es. "CCNL Pubblici Esercizi"
    ore_settimanali: Optional[float] = None
    note:            Optional[str]  = None


@router.post("")
async def crea_dipendente(request: Request, body: NuovoDipendente):
    verify_token(request)
    cf = body.codice_fiscale.upper().strip()
    existing = await col_dip().find_one({"codice_fiscale": cf})
    if existing:
        raise HTTPException(status_code=409, detail="Dipendente già presente")

    doc = {
        "_id":             str(uuid.uuid4()),
        "codice_fiscale":  cf,
        "nome":            body.nome,
        "cognome":         body.cognome,
        "qualifica":       body.qualifica or "",
        "livello":         body.livello or "",
        "iban":            body.iban or "",
        "email":           body.email or "",
        "telefono":        body.telefono or "",
        "data_assunzione": body.data_assunzione or "",
        "ccnl":            body.ccnl or "CCNL Pubblici Esercizi",
        "ore_settimanali": body.ore_settimanali or 40.0,
        "note":            body.note or "",
        "attivo":          True,
        "created_at":      datetime.utcnow().isoformat(),
    }
    await col_dip().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── PUT /api/dipendenti/{cf} ──────────────────────────────────────────────────
class AggiornaDipendente(BaseModel):
    nome:            Optional[str]   = None
    cognome:         Optional[str]   = None
    qualifica:       Optional[str]   = None
    livello:         Optional[str]   = None
    iban:            Optional[str]   = None
    email:           Optional[str]   = None
    telefono:        Optional[str]   = None
    data_assunzione: Optional[str]   = None
    ccnl:            Optional[str]   = None
    ore_settimanali: Optional[float] = None
    note:            Optional[str]   = None
    attivo:          Optional[bool]  = None


@router.put("/{cf}")
async def aggiorna_dipendente(request: Request, cf: str, body: AggiornaDipendente):
    verify_token(request)
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nessun campo da aggiornare")
    update["updated_at"] = datetime.utcnow().isoformat()

    r = await col_dip().update_one({"codice_fiscale": cf.upper()}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    return {"ok": True}


# ── DELETE /api/dipendenti/{cf} (cessazione) ──────────────────────────────────
@router.delete("/{cf}")
async def cessa_dipendente(
    request: Request,
    cf: str,
    data_cessazione: Optional[str] = Query(None),
):
    verify_token(request)
    r = await col_dip().update_one(
        {"codice_fiscale": cf.upper()},
        {"$set": {
            "attivo": False,
            "data_cessazione": data_cessazione or datetime.utcnow().strftime("%Y-%m-%d"),
            "updated_at": datetime.utcnow().isoformat(),
        }}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Dipendente non trovato")
    return {"ok": True}


# ── POST /api/dipendenti/bulk-upsert ─────────────────────────────────────────
class DipendenteBulk(BaseModel):
    codice_fiscale:  str
    nome:            str
    cognome:         str
    qualifica:       Optional[str]  = None
    livello:         Optional[str]  = None
    iban:            Optional[str]  = None
    ore_settimanali: Optional[float] = None


@router.post("/bulk-upsert")
async def bulk_upsert_dipendenti(request: Request, body: List[DipendenteBulk]):
    """
    Import bulk da impresasemplice.online o Zucchetti.
    Sostituisce l'URL hardcoded nel vecchio import_dipendenti.html.
    """
    verify_token(request)
    contatori = {"inseriti": 0, "aggiornati": 0, "errori": 0}

    for item in body:
        cf = item.codice_fiscale.upper().strip()
        try:
            r = await col_dip().update_one(
                {"codice_fiscale": cf},
                {"$set": {
                    "nome":            item.nome,
                    "cognome":         item.cognome,
                    "qualifica":       item.qualifica or "",
                    "livello":         item.livello or "",
                    "iban":            item.iban or "",
                    "ore_settimanali": item.ore_settimanali or 40.0,
                    "updated_at":      datetime.utcnow().isoformat(),
                }, "$setOnInsert": {
                    "_id":        str(uuid.uuid4()),
                    "attivo":     True,
                    "created_at": datetime.utcnow().isoformat(),
                }},
                upsert=True,
            )
            if r.upserted_id:
                contatori["inseriti"] += 1
            else:
                contatori["aggiornati"] += 1
        except Exception:
            contatori["errori"] += 1

    return {"ok": True, **contatori}


# ── GET /api/dipendenti/{cf}/presenze ─────────────────────────────────────────
@router.get("/{cf}/presenze")
async def presenze_dipendente(
    request: Request,
    cf: str,
    anno:  int = Query(datetime.now().year),
    mese:  Optional[int] = Query(None),
):
    verify_token(request)
    filtro: dict = {"codice_fiscale": cf.upper(), "anno": anno}
    if mese:
        filtro["mese"] = mese

    cursor = col_pres().find(filtro).sort("data", 1)
    docs   = await cursor.to_list(length=400)
    return {"presenze": [_ser(d) for d in docs], "totale": len(docs)}


# ── POST /api/dipendenti/{cf}/presenze ────────────────────────────────────────
class PresenzaEntry(BaseModel):
    data:   str          # YYYY-MM-DD
    codice: str          # AI=assenza ingiustificata, MA=malattia, FE=ferie, ORD=ordinario, STR=straordinario
    ore:    float = 8.0
    note:   Optional[str] = None


@router.post("/{cf}/presenze")
async def registra_presenza(request: Request, cf: str, body: PresenzaEntry):
    verify_token(request)
    try:
        dt   = datetime.strptime(body.data, "%Y-%m-%d")
        anno = dt.year
        mese = dt.month
    except Exception:
        raise HTTPException(status_code=400, detail="Data non valida (YYYY-MM-DD)")

    doc = {
        "_id":            str(uuid.uuid4()),
        "codice_fiscale": cf.upper(),
        "data":           body.data,
        "anno":           anno,
        "mese":           mese,
        "codice":         body.codice.upper(),
        "ore":            body.ore,
        "note":           body.note or "",
        "created_at":     datetime.utcnow().isoformat(),
    }
    await col_pres().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── GET /api/dipendenti/report/presenze-mese ──────────────────────────────────
@router.get("/report/presenze-mese")
async def report_presenze_mese(
    request: Request,
    anno:    int = Query(datetime.now().year),
    mese:    int = Query(datetime.now().month),
):
    """Riepilogo presenze tutti i dipendenti per mese (per elaborazione paghe)."""
    verify_token(request)
    pipeline = [
        {"$match": {"anno": anno, "mese": mese}},
        {"$group": {
            "_id":  "$codice_fiscale",
            "ore_ordinarie":    {"$sum": {"$cond": [{"$eq": ["$codice", "ORD"]}, "$ore", 0]}},
            "ore_straordinari": {"$sum": {"$cond": [{"$eq": ["$codice", "STR"]}, "$ore", 0]}},
            "giorni_ferie":     {"$sum": {"$cond": [{"$eq": ["$codice", "FE"]},  1, 0]}},
            "giorni_malattia":  {"$sum": {"$cond": [{"$eq": ["$codice", "MA"]},  1, 0]}},
            "giorni_assenza":   {"$sum": {"$cond": [{"$eq": ["$codice", "AI"]},  1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    db     = get_db()
    result = await db["presenze"].aggregate(pipeline).to_list(length=200)
    return {"anno": anno, "mese": mese, "report": result}
