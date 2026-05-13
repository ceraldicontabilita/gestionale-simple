"""
Router Estratto Conto — Ceraldi Group ERP
Import CSV/Excel estratto bancario → riconciliazione automatica con prima nota provvisori.

Formati supportati:
  - UniCredit CSV (separatore ";", encoding latin-1)
  - Banca Nazionale CSV
  - Formato generico (data, descrizione, dare, avere)

Logica riconciliazione:
  Ogni movimento banca cerca un corrispondente nei provvisori per:
    1. Importo uguale ± €0.50
    2. Data entro ±5 giorni
    3. (opzionale) keyword in descrizione
  Se trovato → sposta provvisorio in prima_nota_banca.
"""
import csv
import io
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.database import get_db, col_estratto_conto
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/estratto-conto", tags=["estratto_conto"])

SOGLIA_IMPORTO = 0.50   # €
SOGLIA_GIORNI  = 5


def _safe_float(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip().replace(".", "").replace(",", ".").replace(" ", "").replace("€", "")
    try:
        return abs(float(s))
    except Exception:
        return 0.0


def _parse_date(s: str) -> str:
    """Normalizza data in YYYY-MM-DD da vari formati."""
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return s


def _parse_unicredit(content: str) -> List[dict]:
    """Parser CSV UniCredit: Data operazione;Data valuta;Descrizione;Accrediti;Addebiti"""
    movimenti = []
    reader = csv.reader(io.StringIO(content), delimiter=";")
    header_found = False
    for row in reader:
        if not row:
            continue
        if not header_found:
            joined = ";".join(row).lower()
            if "data" in joined and ("descrizione" in joined or "causale" in joined):
                header_found = True
            continue
        try:
            data        = _parse_date(row[0]) if len(row) > 0 else ""
            descrizione = row[2].strip()       if len(row) > 2 else ""
            accredito   = _safe_float(row[3])  if len(row) > 3 else 0.0
            addebito    = _safe_float(row[4])  if len(row) > 4 else 0.0

            if not data:
                continue

            if accredito > 0:
                movimenti.append({
                    "data": data, "descrizione": descrizione,
                    "tipo": "entrata", "importo": accredito,
                })
            if addebito > 0:
                movimenti.append({
                    "data": data, "descrizione": descrizione,
                    "tipo": "uscita", "importo": addebito,
                })
        except Exception:
            continue
    return movimenti


def _parse_bpm(content: str) -> List[dict]:
    """Parser CSV Banco BPM.
    Header: Ragione Sociale;Data contabile;Data valuta;Banca;Rapporto;Importo;Divisa;Descrizione;Categoria/sottocategoria;Hashtag
    Indici:  0              1              2            3     4        5       6      7            8                       9
    Importo è con segno: negativo=uscita, positivo=entrata. Decimali con virgola.
    """
    movimenti = []
    reader = csv.reader(io.StringIO(content), delimiter=";", quotechar='"')
    rows = list(reader)
    for row in rows[1:]:   # salta header
        if len(row) < 8:
            continue
        try:
            raw_date = row[2].strip() or row[1].strip()
            data = _parse_date(raw_date)
            if not data:
                continue
            importo_raw = row[5].strip().replace('"', '').replace('\xa0', '').replace(' ', '')
            if not importo_raw:
                continue
            # Formato europeo: punti come separatore migliaia, virgola come decimale
            importo_signed = float(importo_raw.replace('.', '').replace(',', '.'))
            if importo_signed == 0:
                continue
            tipo    = "entrata" if importo_signed > 0 else "uscita"
            importo = abs(importo_signed)
            descrizione = row[7].strip() if len(row) > 7 else ""
            categoria   = row[8].strip() if len(row) > 8 else ""
            movimenti.append({
                "data":        data,
                "descrizione": descrizione,
                "tipo":        tipo,
                "importo":     importo,
                "categoria":   categoria,
            })
        except Exception:
            continue
    return movimenti


def _parse_generic(content: str) -> List[dict]:
    """Parser CSV generico: prova separatori , ; tab"""
    for sep in (";", ",", "\t"):
        lines = [l for l in content.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        reader = csv.reader(io.StringIO(content), delimiter=sep)
        rows   = list(reader)
        if len(rows[0]) < 3:
            continue

        movimenti = []
        for row in rows[1:]:
            if len(row) < 3:
                continue
            try:
                data  = _parse_date(row[0])
                desc  = row[1].strip() if len(row) > 1 else ""
                dare  = _safe_float(row[2]) if len(row) > 2 else 0.0
                avere = _safe_float(row[3]) if len(row) > 3 else 0.0
                if not data:
                    continue
                if dare > 0:
                    movimenti.append({"data": data, "descrizione": desc, "tipo": "uscita",  "importo": dare})
                if avere > 0:
                    movimenti.append({"data": data, "descrizione": desc, "tipo": "entrata", "importo": avere})
            except Exception:
                continue
        if movimenti:
            return movimenti
    return []


async def _riconcilia_movimento(db, mov_id: str, mov: dict) -> dict:
    """
    Cerca nei provvisori un match per importo ± SOGLIA e data ± SOGLIA_GIORNI.
    Se trovato → lo sposta in prima_nota_banca e ritorna il match.
    """
    importo = mov["importo"]
    tipo    = mov["tipo"]

    try:
        dt   = datetime.strptime(mov["data"], "%Y-%m-%d")
        d1   = (dt - timedelta(days=SOGLIA_GIORNI)).strftime("%Y-%m-%d")
        d2   = (dt + timedelta(days=SOGLIA_GIORNI)).strftime("%Y-%m-%d")
    except Exception:
        return {"riconciliato": False}

    # Cerca nei provvisori
    filtro = {
        "tipo":    tipo,
        "stato":   {"$in": ["provvisorio", "pending"]},
        "importo": {"$gte": importo - SOGLIA_IMPORTO, "$lte": importo + SOGLIA_IMPORTO},
        "$or": [
            {"data":     {"$gte": d1, "$lte": d2}},
            {"data":     {"$exists": False}},
        ],
    }
    prov = await db["prima_nota_provvisori"].find_one(filtro)

    if prov:
        prov_id = prov["_id"]
        # Sposta in prima nota banca
        banca_doc = {
            "_id":          new_id(),
            "tipo":         tipo,
            "importo":      importo,
            "data":         mov["data"],
            "descrizione":  mov["descrizione"],
            "categoria":    prov.get("categoria", "riconciliato"),
            "estratto_id":  mov_id,
            "prov_id":      str(prov_id),
            "riconciliato": True,
            "created_at":   datetime.utcnow().isoformat(),
        }
        # Copia campi specifici (f24_id, fattura_id, ecc.)
        for k in ("f24_id", "fattura_id", "verbale_id", "corrispettivo_id"):
            if prov.get(k):
                banca_doc[k] = prov[k]

        await db["prima_nota_banca"].insert_one(banca_doc)
        await db["prima_nota_provvisori"].delete_one({"_id": prov_id})

        return {
            "riconciliato":  True,
            "prov_id":       str(prov_id),
            "banca_doc_id":  banca_doc["_id"],
            "prov_descrizione": prov.get("descrizione", ""),
        }
    return {"riconciliato": False}


# ── POST /api/estratto-conto/import ──────────────────────────────────────────
@router.post("/import")
async def import_estratto(
    request:    Request,
    file:       UploadFile = File(...),
    auto_riconcilia: bool  = True,
    banca:      str        = "unicredit",
):
    """
    Importa estratto conto CSV.
    auto_riconcilia=True → cerca automaticamente match nei provvisori.
    """
    verify_token(request)
    raw = await file.read()

    # Prova encoding UTF-8, poi latin-1
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            content = raw.decode(enc)
            break
        except Exception:
            pass
    else:
        raise HTTPException(status_code=400, detail="Encoding file non riconosciuto")

    # Parse
    fname_lower = file.filename.lower()
    if banca == "bpm" or "bpm" in fname_lower or "bancobpm" in fname_lower:
        movimenti = _parse_bpm(content)
    elif banca == "unicredit" or "unicredit" in fname_lower:
        movimenti = _parse_unicredit(content)
    else:
        movimenti = _parse_generic(content)
        # Se generic non produce nulla, prova BPM come fallback
        if not movimenti:
            movimenti = _parse_bpm(content)

    if not movimenti:
        raise HTTPException(status_code=422, detail="Nessun movimento estratto dal file")

    db = get_db()
    inseriti     = 0
    saltati      = 0
    riconciliati = 0

    for mov in movimenti:
        # Dedup per data + importo + descrizione
        existing = await col_estratto_conto().find_one({
            "data":        mov["data"],
            "importo":     mov["importo"],
            "descrizione": mov["descrizione"],
            "tipo":        mov["tipo"],
        })
        if existing:
            saltati += 1
            continue

        mov_id = new_id()
        doc = {
            "_id":          mov_id,
            "data":         mov["data"],
            "descrizione":  mov["descrizione"],
            "tipo":         mov["tipo"],
            "importo":      mov["importo"],
            "banca":        banca,
            "filename":     file.filename,
            "riconciliato": False,
            "created_at":   datetime.utcnow().isoformat(),
        }

        if auto_riconcilia:
            match = await _riconcilia_movimento(db, mov_id, mov)
            doc["riconciliato"]   = match.get("riconciliato", False)
            doc["prov_id"]        = match.get("prov_id", "")
            doc["banca_doc_id"]   = match.get("banca_doc_id", "")
            if match.get("riconciliato"):
                riconciliati += 1

        await col_estratto_conto().insert_one(doc)
        inseriti += 1

    return {
        "ok":           True,
        "movimenti":    len(movimenti),
        "inseriti":     inseriti,
        "saltati":      saltati,
        "riconciliati": riconciliati,
        "non_riconciliati": inseriti - riconciliati,
    }


# ── GET /api/estratto-conto ───────────────────────────────────────────────────
@router.get("")
async def lista_estratto(
    request:      Request,
    riconciliato: Optional[bool] = Query(None),
    tipo:         Optional[str]  = Query(None),
    data_da:      Optional[str]  = Query(None),
    data_a:       Optional[str]  = Query(None),
    limit:        int            = Query(500),
):
    verify_token(request)
    filtro: dict = {}
    if riconciliato is not None:
        filtro["riconciliato"] = riconciliato
    if tipo:
        filtro["tipo"] = tipo
    if data_da or data_a:
        filtro["data"] = {}
        if data_da:
            filtro["data"]["$gte"] = data_da
        if data_a:
            filtro["data"]["$lte"] = data_a

    cursor = col_estratto_conto().find(filtro).sort("data", -1).limit(limit)
    docs   = await cursor.to_list(length=limit)
    return {"movimenti": [_ser(d) for d in docs], "totale": len(docs)}


# ── POST /api/estratto-conto/{id}/riconcilia-manuale ─────────────────────────
class RiconciliaManuale(BaseModel):
    prov_id:     Optional[str] = None
    categoria:   Optional[str] = None
    descrizione: Optional[str] = None
    note:        Optional[str] = None


@router.post("/{mov_id}/riconcilia-manuale")
async def riconcilia_manuale(request: Request, mov_id: str, body: RiconciliaManuale):
    """Riconcilia manualmente un movimento estratto conto."""
    verify_token(request)
    db  = get_db()
    mov = await col_estratto_conto().find_one({"_id": mov_id})
    if not mov:
        raise HTTPException(status_code=404, detail="Movimento non trovato")

    banca_doc = {
        "_id":          new_id(),
        "tipo":         mov["tipo"],
        "importo":      mov["importo"],
        "data":         mov["data"],
        "descrizione":  body.descrizione or mov["descrizione"],
        "categoria":    body.categoria or "altro",
        "estratto_id":  mov_id,
        "prov_id":      body.prov_id or "",
        "note":         body.note or "",
        "riconciliato": True,
        "created_at":   datetime.utcnow().isoformat(),
    }
    await db["prima_nota_banca"].insert_one(banca_doc)

    # Se aveva un prov_id → cancella il provvisorio
    if body.prov_id:
        await db["prima_nota_provvisori"].delete_one({"_id": body.prov_id})

    # Aggiorna estratto
    await col_estratto_conto().update_one(
        {"_id": mov_id},
        {"$set": {"riconciliato": True, "banca_doc_id": banca_doc["_id"]}}
    )
    return {"ok": True, "banca_doc_id": banca_doc["_id"]}


# ── POST /api/estratto-conto/riconcilia-tutti ─────────────────────────────────
@router.post("/riconcilia-tutti")
async def riconcilia_tutti(request: Request):
    """Riprova la riconciliazione automatica su tutti i movimenti non ancora riconciliati."""
    verify_token(request)
    db = get_db()
    cursor = col_estratto_conto().find({"riconciliato": False}).sort("data", 1)
    docs   = await cursor.to_list(length=2000)

    riconciliati = 0
    for doc in docs:
        match = await _riconcilia_movimento(db, str(doc["_id"]), doc)
        if match.get("riconciliato"):
            await col_estratto_conto().update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "riconciliato": True,
                    "prov_id":      match.get("prov_id", ""),
                    "banca_doc_id": match.get("banca_doc_id", ""),
                }}
            )
            riconciliati += 1

    return {
        "ok":            True,
        "esaminati":     len(docs),
        "riconciliati":  riconciliati,
        "rimasti":       len(docs) - riconciliati,
    }
