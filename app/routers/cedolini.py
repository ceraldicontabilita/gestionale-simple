"""
Router Cedolini / Paghe / F24 — Ceraldi Group ERP
- Upload PDF Zucchetti → parse AI → inserimento cedolini + prima nota salari
- Upload F24 → parse AI → prima nota provvisori banca
- Riconciliazione F24 con estratto conto per codice tributo
- Gmail scan per mittenti attendibili (studio paghe)
"""
import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import get_db

router = APIRouter(prefix="/api/cedolini", tags=["cedolini"])


def _ser(doc):
    if not doc:
        return {}
    from bson import ObjectId
    return {k: str(v) if isinstance(v, ObjectId) else v for k, v in doc.items()}


# ── POST /api/cedolini/upload-libro-unico ─────────────────────────────────────
@router.post("/upload-libro-unico")
async def upload_libro_unico(
    request: Request,
    file:    UploadFile = File(...),
):
    """
    Carica PDF Libro Unico (Zucchetti).
    Claude estrae tutti i cedolini e li inserisce in DB.
    """
    verify_token(request)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF accettati")

    pdf_bytes = await file.read()
    from app.services.ai_parser import parse_cedolino

    try:
        dati = await parse_cedolino(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Errore parsing PDF: {e}")

    db = get_db()
    inseriti = 0
    saltati  = 0

    dipendenti = dati.get("dipendenti", [])
    mese_rif   = dati.get("mese_riferimento", "")
    anno       = dati.get("anno", datetime.now().year)

    for dip in dipendenti:
        cf = dip.get("codice_fiscale", "").upper().strip()
        if not cf:
            continue

        # Dedup per CF + mese
        existing = await db["cedolini"].find_one({
            "codice_fiscale":    cf,
            "mese_riferimento":  mese_rif,
        })
        if existing:
            saltati += 1
            continue

        # Upsert dipendente anagrafica
        await db["dipendenti"].update_one(
            {"codice_fiscale": cf},
            {"$set": {
                "nome":     dip.get("nome", ""),
                "cognome":  dip.get("cognome", ""),
                "iban":     dip.get("iban", ""),
                "qualifica": dip.get("qualifica", ""),
                "livello":  dip.get("livello", ""),
                "updated_at": datetime.utcnow().isoformat(),
            }, "$setOnInsert": {
                "_id": str(uuid.uuid4()),
                "attivo": True,
                "created_at": datetime.utcnow().isoformat(),
            }},
            upsert=True,
        )

        cedolino_doc = {
            "_id":               str(uuid.uuid4()),
            "codice_fiscale":    cf,
            "nome":              dip.get("nome", ""),
            "cognome":           dip.get("cognome", ""),
            "mese_riferimento":  mese_rif,
            "anno":              anno,
            "retribuzione_lorda": dip.get("retribuzione_lorda", 0.0),
            "netto_pagare":      dip.get("netto_pagare", 0.0),
            "trattenute":        dip.get("trattenute", []),
            "presenze":          dip.get("presenze", {}),
            "voci_paga":         dip.get("voci_paga", []),
            "iban":              dip.get("iban", ""),
            "source":            "upload_pdf",
            "created_at":        datetime.utcnow().isoformat(),
        }
        await db["cedolini"].insert_one(cedolino_doc)
        inseriti += 1

    # Prima nota salari (totale netti → uscita banca)
    totale_netti = dati.get("totale_netti", 0.0)
    if totale_netti > 0 and mese_rif:
        existing_pn = await db["prima_nota_banca"].find_one({
            "categoria": "salari",
            "mese_riferimento": mese_rif,
        })
        if not existing_pn:
            await db["prima_nota_banca"].insert_one({
                "_id":               str(uuid.uuid4()),
                "tipo":              "uscita",
                "importo":           totale_netti,
                "data":              datetime.utcnow().strftime("%Y-%m-%d"),
                "descrizione":       f"Pagamento salari {mese_rif} — {len(dipendenti)} dip.",
                "categoria":         "salari",
                "mese_riferimento":  mese_rif,
                "created_at":        datetime.utcnow().isoformat(),
            })

    return {
        "ok":          True,
        "mese":        mese_rif,
        "dipendenti":  len(dipendenti),
        "inseriti":    inseriti,
        "saltati":     saltati,
        "totale_netti": totale_netti,
        "totale_lordo": dati.get("totale_lordo", 0.0),
    }


# ── POST /api/cedolini/upload-f24 ────────────────────────────────────────────
@router.post("/upload-f24")
async def upload_f24(
    request: Request,
    file:    UploadFile = File(...),
):
    """
    Carica PDF modello F24.
    Claude estrae i codici tributo e crea movimento provvisorio in prima nota banca.
    """
    verify_token(request)
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF accettati")

    pdf_bytes = await file.read()
    from app.services.ai_parser import parse_f24

    try:
        dati = await parse_f24(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Errore parsing F24: {e}")

    db       = get_db()
    scadenza = dati.get("scadenza", "")
    totale   = dati.get("totale_saldo", 0.0)
    periodo  = dati.get("periodo", "")
    codici   = dati.get("codici_tributo", [])

    # Salva F24
    f24_id = str(uuid.uuid4())
    await db["f24_commercialista"].insert_one({
        "_id":            f24_id,
        "scadenza":       scadenza,
        "periodo":        periodo,
        "totale":         totale,
        "codici_tributo": codici,
        "stato":          "da_pagare",
        "source":         "upload_pdf",
        "filename":       file.filename,
        "created_at":     datetime.utcnow().isoformat(),
    })

    # Crea provvisorio banca
    if totale > 0:
        descrizione_codici = " | ".join(
            f"{c.get('codice_tributo','')} €{c.get('importo_debito',0):.2f}"
            for c in codici if c.get("importo_debito", 0) > 0
        )
        await db["prima_nota_provvisori"].insert_one({
            "_id":          str(uuid.uuid4()),
            "tipo":         "uscita",
            "importo":      totale,
            "data":         scadenza or datetime.utcnow().strftime("%Y-%m-%d"),
            "descrizione":  f"F24 scad. {scadenza} | {descrizione_codici}",
            "categoria":    "f24_paghe",
            "f24_id":       f24_id,
            "stato":        "provvisorio",
            "codici_tributo_cerca": [c.get("codice_tributo") for c in codici if c.get("codice_tributo")],
            "created_at":   datetime.utcnow().isoformat(),
        })

    return {
        "ok":       True,
        "f24_id":   f24_id,
        "scadenza": scadenza,
        "periodo":  periodo,
        "totale":   totale,
        "codici":   len(codici),
        "dettaglio_codici": codici,
    }


# ── GET /api/cedolini ─────────────────────────────────────────────────────────
@router.get("")
async def lista_cedolini(
    request: Request,
    anno:    int          = Query(datetime.now().year),
    mese:    Optional[str] = Query(None),
    cf:      Optional[str] = Query(None),
):
    verify_token(request)
    db     = get_db()
    filtro: dict = {"anno": anno}
    if mese:
        filtro["mese_riferimento"] = mese
    if cf:
        filtro["codice_fiscale"] = cf.upper()

    cursor = db["cedolini"].find(filtro).sort([("mese_riferimento", -1), ("cognome", 1)])
    docs   = await cursor.to_list(length=500)
    return {"cedolini": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/cedolini/riepilogo/{anno} ────────────────────────────────────────
@router.get("/riepilogo/{anno}")
async def riepilogo_annuale(request: Request, anno: int):
    """Riepilogo costi del personale per mese."""
    verify_token(request)
    db = get_db()
    pipeline = [
        {"$match": {"anno": anno}},
        {"$group": {
            "_id": "$mese_riferimento",
            "totale_lordo":  {"$sum": "$retribuzione_lorda"},
            "totale_netti":  {"$sum": "$netto_pagare"},
            "n_dipendenti":  {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    mesi   = await db["cedolini"].aggregate(pipeline).to_list(length=12)
    totali = {
        "lordo": sum(m["totale_lordo"] for m in mesi),
        "netti": sum(m["totale_netti"] for m in mesi),
    }
    return {"anno": anno, "mesi": mesi, "totali_anno": totali}


# ── GET /api/cedolini/f24 ─────────────────────────────────────────────────────
@router.get("/f24")
async def lista_f24(
    request: Request,
    anno:    int          = Query(datetime.now().year),
    stato:   Optional[str] = Query(None),   # da_pagare | pagato
):
    verify_token(request)
    db     = get_db()
    filtro: dict = {}
    # Filtra per anno da scadenza o periodo
    if anno:
        filtro["$or"] = [
            {"scadenza": {"$regex": f"^{anno}"}},
            {"periodo":  {"$regex": str(anno)}},
        ]
    if stato:
        filtro["stato"] = stato

    cursor = db["f24_commercialista"].find(filtro).sort("scadenza", -1)
    docs   = await cursor.to_list(length=200)
    return {"f24": [_ser(d) for d in docs], "totale": len(docs)}


# ── POST /api/cedolini/f24/{id}/riconcilia ────────────────────────────────────
class RiconciliaF24Request(BaseModel):
    data_pagamento:    str
    estratto_conto_id: Optional[str] = None
    note:              Optional[str] = None


@router.post("/f24/{f24_id}/riconcilia")
async def riconcilia_f24(
    request: Request,
    f24_id: str,
    body:   RiconciliaF24Request,
):
    """
    Marca F24 come pagato e riconcilia il provvisorio in prima nota banca.
    Viene chiamato quando trovi il movimento nell'estratto conto
    (ricercando per codice tributo 1001/5100/3802/3847/3848 o importo).
    """
    verify_token(request)
    db  = get_db()
    f24 = await db["f24_commercialista"].find_one({"_id": f24_id})
    if not f24:
        raise HTTPException(status_code=404, detail="F24 non trovato")

    totale = f24.get("totale", 0.0)

    # Aggiorna F24
    await db["f24_commercialista"].update_one(
        {"_id": f24_id},
        {"$set": {
            "stato":           "pagato",
            "data_pagamento":  body.data_pagamento,
            "estratto_id":     body.estratto_conto_id or "",
            "updated_at":      datetime.utcnow().isoformat(),
        }}
    )

    # Sposta provvisorio → banca
    prov = await db["prima_nota_provvisori"].find_one({"f24_id": f24_id})
    if prov:
        prov_id = prov["_id"]
        # Inserisci in prima nota banca
        await db["prima_nota_banca"].insert_one({
            "_id":          str(uuid.uuid4()),
            "tipo":         "uscita",
            "importo":      totale,
            "data":         body.data_pagamento,
            "descrizione":  prov.get("descrizione", f"F24 pagato {body.data_pagamento}"),
            "categoria":    "f24_paghe",
            "f24_id":       f24_id,
            "prov_id":      str(prov_id),
            "note":         body.note or "",
            "created_at":   datetime.utcnow().isoformat(),
        })
        # Cancella provvisorio
        await db["prima_nota_provvisori"].delete_one({"_id": prov_id})

    return {"ok": True, "f24_id": f24_id, "stato": "pagato", "importo": totale}


# ── POST /api/cedolini/gmail-scan ────────────────────────────────────────────
class GmailScanRequest(BaseModel):
    giorni_indietro:  int  = 14
    forza_riprocessa: bool = False


@router.post("/gmail-scan")
async def gmail_scan(request: Request, body: GmailScanRequest = GmailScanRequest()):
    """
    Avvia scansione Gmail per allegati PDF da studio paghe e altri mittenti attendibili.
    Mittenti supportati: f.ferrantini@email.it, grazia.studioferrantini@email.it, ecc.
    """
    verify_token(request)
    from app.services.gmail_scanner import scan_gmail
    result = await scan_gmail(
        giorni_indietro=body.giorni_indietro,
        forza_riprocessa=body.forza_riprocessa,
    )
    return result


# ── POST /api/cedolini/documenti/{doc_id}/processa ───────────────────────────
@router.post("/documenti/{doc_id}/processa")
async def processa_documento(request: Request, doc_id: str):
    """Riprocessa manualmente un documento PDF già in inbox."""
    verify_token(request)
    from app.services.gmail_scanner import processa_documento_inbox
    result = await processa_documento_inbox(doc_id)
    return result


# ── GET /api/cedolini/documenti ───────────────────────────────────────────────
@router.get("/documenti")
async def lista_documenti_inbox(
    request:    Request,
    processato: Optional[bool] = Query(None),
    tipo:       Optional[str]  = Query(None),
    limit:      int            = Query(50),
):
    """Lista documenti ricevuti da Gmail (Libro Unico, F24, CU, ecc.)."""
    verify_token(request)
    db     = get_db()
    filtro: dict = {}
    if processato is not None:
        filtro["processato"] = processato
    if tipo:
        filtro["tipo"] = tipo

    # Non restituire i bytes PDF nella lista
    cursor = db["documents_inbox"].find(filtro, {"pdf_bytes_b64": 0}).sort("created_at", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)
    for d in docs:
        from bson import ObjectId
        if isinstance(d.get("_id"), ObjectId):
            d["_id"] = str(d["_id"])
    return {"documenti": docs, "totale": len(docs)}


# ── GET /api/cedolini/f24/{f24_id}/cerca-estratto ────────────────────────────
@router.get("/f24/{f24_id}/cerca-estratto")
async def cerca_f24_estratto(request: Request, f24_id: str):
    """
    Cerca nell'estratto conto il pagamento F24 cercando per:
    - importo esatto ± €0.50
    - data vicina alla scadenza ± 5 giorni
    - descrizione contenente codici tributo o "F24"
    """
    verify_token(request)
    db  = get_db()
    f24 = await db["f24_commercialista"].find_one({"_id": f24_id})
    if not f24:
        raise HTTPException(status_code=404, detail="F24 non trovato")

    totale   = f24.get("totale", 0.0)
    scadenza = f24.get("scadenza", "")
    codici   = [c.get("codice_tributo", "") for c in f24.get("codici_tributo", [])]

    filtro: dict = {
        "importo": {"$gte": totale - 0.5, "$lte": totale + 0.5},
        "tipo": "uscita",
    }
    if scadenza:
        try:
            from datetime import timedelta
            dt  = datetime.strptime(scadenza, "%Y-%m-%d")
            d1  = (dt - timedelta(days=5)).strftime("%Y-%m-%d")
            d2  = (dt + timedelta(days=5)).strftime("%Y-%m-%d")
            filtro["data"] = {"$gte": d1, "$lte": d2}
        except Exception:
            pass

    # Cerca in estratto conto
    cursor  = db["estratto_conto"].find(filtro).sort("data", 1).limit(10)
    matches = await cursor.to_list(length=10)

    # Cerca anche per keyword F24 o codici tributo
    keyword_filter = {
        "descrizione": {"$regex": "|".join(["F24"] + [c for c in codici if c]), "$options": "i"},
        "tipo": "uscita",
    }
    cursor2  = db["estratto_conto"].find(keyword_filter).sort("data", -1).limit(5)
    matches2 = await cursor2.to_list(length=5)

    # Unifica deduplicando per _id
    seen = set()
    tutti = []
    for m in (matches + matches2):
        k = str(m.get("_id", ""))
        if k not in seen:
            seen.add(k)
            tutti.append(_ser(m))

    return {
        "f24_id":    f24_id,
        "totale":    totale,
        "scadenza":  scadenza,
        "matches":   tutti,
        "trovati":   len(tutti),
    }
