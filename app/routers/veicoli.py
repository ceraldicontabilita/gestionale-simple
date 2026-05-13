"""
Router Veicoli/Flotta — Ceraldi Group ERP
Anagrafica flotta aziendale: targhe, tipo contratto (proprietà/noleggio Leasys/ALD),
scadenze bollo/revisione/assicurazione, abbinamento fatture noleggio.
"""
from datetime import datetime, date, timedelta
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import get_db
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/veicoli", tags=["veicoli"])


def col_v():
    return get_db()["veicoli_noleggio"]


# ── GET /api/veicoli ──────────────────────────────────────────────────────────
@router.get("")
async def lista_veicoli(
    request:  Request,
    attivo:   Optional[bool] = Query(None),
    cerca:    Optional[str]  = Query(None),
    tipo_contratto: Optional[str] = Query(None),  # proprieta | noleggio_llt | noleggio_breve
):
    verify_token(request)
    filtro: dict = {}
    if attivo is not None:
        filtro["attivo"] = attivo
    if cerca:
        filtro["$or"] = [
            {"targa":   {"$regex": cerca, "$options": "i"}},
            {"modello": {"$regex": cerca, "$options": "i"}},
            {"marca":   {"$regex": cerca, "$options": "i"}},
        ]
    if tipo_contratto:
        filtro["tipo_contratto"] = tipo_contratto

    cursor = col_v().find(filtro).sort("targa", 1)
    docs   = await cursor.to_list(length=200)

    # Aggiunge flag scadenze imminenti (entro 30 giorni)
    oggi  = date.today()
    entro = oggi + timedelta(days=30)
    for d in docs:
        d["alert_scadenze"] = []
        for campo, label in [
            ("scadenza_bollo",      "Bollo"),
            ("scadenza_revisione",  "Revisione"),
            ("scadenza_assicurazione", "Assicurazione"),
            ("fine_contratto_noleggio", "Fine noleggio"),
        ]:
            val = d.get(campo, "")
            if val:
                try:
                    dt = datetime.strptime(val[:10], "%Y-%m-%d").date()
                    if dt <= entro:
                        d["alert_scadenze"].append({
                            "tipo":     label,
                            "scadenza": val[:10],
                            "giorni":   (dt - oggi).days,
                        })
                except Exception:
                    pass

    return {"veicoli": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/veicoli/{targa} ──────────────────────────────────────────────────
@router.get("/{targa}")
async def dettaglio_veicolo(request: Request, targa: str):
    verify_token(request)
    doc = await col_v().find_one({"targa": targa.upper()})
    if not doc:
        raise HTTPException(status_code=404, detail="Veicolo non trovato")

    db = get_db()
    # Ultime 10 fatture noleggio abbinate
    cursor  = db["invoices"].find({"targhe_veicoli": targa.upper()}).sort("data_fattura", -1).limit(10)
    fatture = await cursor.to_list(length=10)

    # Verbali abbinati alla targa
    cursor2  = db["verbali_noleggio"].find({"targa": targa.upper(), "stato": {"$ne": "eliminato"}}).sort("data_verbale", -1).limit(10)
    verbali  = await cursor2.to_list(length=10)

    return {
        "veicolo":  _ser(doc),
        "fatture":  [_ser(f) for f in fatture],
        "verbali":  [_ser(v) for v in verbali],
    }


# ── POST /api/veicoli ─────────────────────────────────────────────────────────
class NuovoVeicolo(BaseModel):
    targa:          str
    marca:          str
    modello:        str
    anno:           Optional[int]   = None
    tipo_contratto: str = "noleggio_llt"  # proprieta | noleggio_llt | noleggio_breve
    fornitore_noleggio: Optional[str] = None   # es. "Leasys", "ALD Automotive"
    fornitore_piva:    Optional[str] = None    # P.IVA Leasys/ALD
    canone_mensile:    Optional[float] = None
    inizio_contratto:  Optional[str] = None    # YYYY-MM-DD
    fine_contratto_noleggio: Optional[str] = None
    km_contratto:      Optional[int]  = None
    scadenza_bollo:    Optional[str]  = None
    scadenza_revisione: Optional[str] = None
    scadenza_assicurazione: Optional[str] = None
    compagnia_assicurazione: Optional[str] = None
    uso:            str = "promiscuo"   # aziendale | promiscuo | personale
    detraibilita_iva_pct: float = 40.0  # noleggio LLT → 40%
    note:           Optional[str] = None


@router.post("")
async def crea_veicolo(request: Request, body: NuovoVeicolo):
    verify_token(request)
    targa = body.targa.upper().replace(" ", "")
    existing = await col_v().find_one({"targa": targa})
    if existing:
        raise HTTPException(status_code=409, detail="Veicolo già presente")

    doc = {
        "_id":           new_id(),
        "targa":         targa,
        "marca":         body.marca,
        "modello":       body.modello,
        "anno":          body.anno,
        "tipo_contratto": body.tipo_contratto,
        "fornitore_noleggio": body.fornitore_noleggio or "",
        "fornitore_piva":    body.fornitore_piva or "",
        "canone_mensile":    body.canone_mensile or 0.0,
        "inizio_contratto":  body.inizio_contratto or "",
        "fine_contratto_noleggio": body.fine_contratto_noleggio or "",
        "km_contratto":      body.km_contratto or 0,
        "scadenza_bollo":    body.scadenza_bollo or "",
        "scadenza_revisione": body.scadenza_revisione or "",
        "scadenza_assicurazione": body.scadenza_assicurazione or "",
        "compagnia_assicurazione": body.compagnia_assicurazione or "",
        "uso":           body.uso,
        "detraibilita_iva_pct": body.detraibilita_iva_pct,
        "note":          body.note or "",
        "attivo":        True,
        "created_at":    datetime.utcnow().isoformat(),
    }
    await col_v().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


# ── PUT /api/veicoli/{targa} ──────────────────────────────────────────────────
class AggiornaVeicolo(BaseModel):
    marca:           Optional[str]   = None
    modello:         Optional[str]   = None
    anno:            Optional[int]   = None
    canone_mensile:  Optional[float] = None
    fine_contratto_noleggio: Optional[str] = None
    scadenza_bollo:  Optional[str]   = None
    scadenza_revisione: Optional[str] = None
    scadenza_assicurazione: Optional[str] = None
    compagnia_assicurazione: Optional[str] = None
    detraibilita_iva_pct: Optional[float] = None
    note:            Optional[str]   = None
    attivo:          Optional[bool]  = None


@router.put("/{targa}")
async def aggiorna_veicolo(request: Request, targa: str, body: AggiornaVeicolo):
    verify_token(request)
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nessun campo da aggiornare")
    update["updated_at"] = datetime.utcnow().isoformat()

    r = await col_v().update_one({"targa": targa.upper()}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Veicolo non trovato")
    return {"ok": True}


# ── GET /api/veicoli/scadenze/prossime ────────────────────────────────────────
@router.get("/scadenze/prossime")
async def scadenze_prossime(
    request: Request,
    giorni:  int = Query(60),
):
    """Veicoli con scadenze (bollo/revisione/assicurazione/fine noleggio) entro N giorni."""
    verify_token(request)
    oggi  = date.today().isoformat()
    entro = (date.today() + timedelta(days=giorni)).isoformat()

    filtro = {
        "attivo": True,
        "$or": [
            {"scadenza_bollo":           {"$gte": oggi, "$lte": entro}},
            {"scadenza_revisione":       {"$gte": oggi, "$lte": entro}},
            {"scadenza_assicurazione":   {"$gte": oggi, "$lte": entro}},
            {"fine_contratto_noleggio":  {"$gte": oggi, "$lte": entro}},
        ]
    }
    cursor = col_v().find(filtro).sort("targa", 1)
    docs   = await cursor.to_list(length=100)

    alerts = []
    for d in docs:
        for campo, label in [
            ("scadenza_bollo",           "Bollo"),
            ("scadenza_revisione",       "Revisione"),
            ("scadenza_assicurazione",   "Assicurazione"),
            ("fine_contratto_noleggio",  "Fine noleggio"),
        ]:
            val = d.get(campo, "")
            if val and oggi <= val[:10] <= entro:
                dt = datetime.strptime(val[:10], "%Y-%m-%d").date()
                alerts.append({
                    "targa":    d["targa"],
                    "marca":    d.get("marca", ""),
                    "modello":  d.get("modello", ""),
                    "tipo":     label,
                    "scadenza": val[:10],
                    "giorni":   (dt - date.today()).days,
                })

    alerts.sort(key=lambda x: x["giorni"])
    return {"alerts": alerts, "totale": len(alerts)}
