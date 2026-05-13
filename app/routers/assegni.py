"""
Router Assegni — Ceraldi Group ERP
Gestione carnet assegni: generazione, compilazione, emissione, incasso.
Automazione: sync da estratto conto + auto-match 4 livelli con fatture.
"""
import re
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query, Body

from app.routers.auth import verify_token
from app.database import get_db, col_assegni, col_invoices, col_pn_banca, col_estratto_conto
from app.utils import serialize_doc as _ser, new_id

router = APIRouter(prefix="/api/assegni", tags=["assegni"])

ASSEGNO_STATI = {
    "vuoto":      {"label": "Vuoto",     "color": "#9e9e9e"},
    "compilato":  {"label": "Compilato", "color": "#2196f3"},
    "emesso":     {"label": "Emesso",    "color": "#ff9800"},
    "incassato":  {"label": "Incassato", "color": "#4caf50"},
    "annullato":  {"label": "Annullato", "color": "#f44336"},
    "scaduto":    {"label": "Scaduto",   "color": "#795548"},
}


# ── GET /api/assegni/stati ────────────────────────────────────────────────────
@router.get("/stati")
async def get_stati(request: Request):
    verify_token(request)
    return ASSEGNO_STATI


# ── GET /api/assegni/stats ────────────────────────────────────────────────────
@router.get("/stats")
async def get_stats(request: Request):
    verify_token(request)
    db = get_db()
    pipeline = [
        {"$match": {"entity_status": {"$ne": "deleted"}}},
        {"$group": {
            "_id": "$stato",
            "count": {"$sum": 1},
            "totale": {"$sum": {"$ifNull": ["$importo", 0]}}
        }}
    ]
    rows = await db["assegni"].aggregate(pipeline).to_list(20)
    totale = await db["assegni"].count_documents({"entity_status": {"$ne": "deleted"}})
    return {
        "totale": totale,
        "per_stato": {r["_id"]: {"count": r["count"], "totale": r["totale"]} for r in rows}
    }


# ── POST /api/assegni/genera ──────────────────────────────────────────────────
@router.post("/genera")
async def genera_assegni(
    request: Request,
    numero_primo: str = Body(..., description="Es. 0208769182-11"),
    quantita:     int = Body(10, ge=1, le=100),
):
    """Genera N assegni sequenziali dal numero fornito."""
    verify_token(request)

    if "-" not in numero_primo:
        raise HTTPException(status_code=400, detail="Formato errato. Usa PREFIX-NUMERO (es. 0208769182-11)")

    parts = numero_primo.rsplit("-", 1)
    prefix = parts[0]
    try:
        start_num = int(parts[1])
    except ValueError:
        raise HTTPException(status_code=400, detail="La parte numerica deve essere un intero")

    # Controlla duplicati
    numeri = [f"{prefix}-{start_num + i}" for i in range(quantita)]
    existing = await col_assegni().count_documents({"numero": {"$in": numeri}})
    if existing:
        raise HTTPException(status_code=400, detail="Alcuni numeri già presenti in DB")

    now = datetime.now(timezone.utc).isoformat()
    docs = []
    for numero in numeri:
        docs.append({
            "_id":               new_id(),
            "numero":            numero,
            "stato":             "vuoto",
            "importo":           None,
            "beneficiario":      None,
            "causale":           None,
            "data_emissione":    None,
            "data_scadenza":     None,
            "numero_fattura":    None,
            "fattura_collegata": None,
            "fornitore_piva":    None,
            "note":              None,
            "prima_nota_banca_id": None,
            "entity_status":     "active",
            "created_at":        now,
            "updated_at":        now,
        })

    await col_assegni().insert_many(docs)
    return {
        "ok": True,
        "generati": quantita,
        "primo": numeri[0],
        "ultimo": numeri[-1],
    }


# ── GET /api/assegni ──────────────────────────────────────────────────────────
@router.get("")
async def lista_assegni(
    request:       Request,
    stato:         Optional[str] = Query(None),
    fornitore_piva: Optional[str] = Query(None),
    cerca:         Optional[str] = Query(None),
    limit:         int           = Query(200),
    skip:          int           = Query(0),
):
    verify_token(request)
    filtro: dict = {"entity_status": {"$ne": "deleted"}}
    if stato:
        filtro["stato"] = stato
    if fornitore_piva:
        filtro["fornitore_piva"] = fornitore_piva
    if cerca:
        filtro["$or"] = [
            {"numero":      {"$regex": cerca, "$options": "i"}},
            {"beneficiario": {"$regex": cerca, "$options": "i"}},
        ]

    cursor = col_assegni().find(filtro).sort([("stato", 1), ("numero", 1)]).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return {"assegni": [_ser(d) for d in docs], "totale": len(docs)}


# ── GET /api/assegni/{id} ─────────────────────────────────────────────────────
@router.get("/{assegno_id}")
async def get_assegno(request: Request, assegno_id: str):
    verify_token(request)
    doc = await col_assegni().find_one({"$or": [{"_id": assegno_id}, {"numero": assegno_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Assegno non trovato")
    return _ser(doc)


# ── PUT /api/assegni/{id} ─────────────────────────────────────────────────────
@router.put("/{assegno_id}")
async def update_assegno(request: Request, assegno_id: str, data: dict = Body(...)):
    verify_token(request)
    data.pop("_id", None)
    data.pop("numero", None)
    data.pop("created_at", None)

    if "stato" in data and data["stato"] not in ASSEGNO_STATI:
        raise HTTPException(status_code=400, detail=f"Stato non valido: {list(ASSEGNO_STATI)}")

    # Auto-compilato se importo + beneficiario settati
    doc = await col_assegni().find_one({"$or": [{"_id": assegno_id}, {"numero": assegno_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Assegno non trovato")
    if doc.get("stato") == "vuoto" and "importo" not in data and "beneficiario" not in data:
        pass
    elif data.get("importo") and data.get("beneficiario") and doc.get("stato") == "vuoto":
        data.setdefault("stato", "compilato")

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    await col_assegni().update_one(
        {"$or": [{"_id": assegno_id}, {"numero": assegno_id}]},
        {"$set": data}
    )
    return {"ok": True}


# ── POST /api/assegni/{id}/collega-fattura ────────────────────────────────────
@router.post("/{assegno_id}/collega-fattura")
async def collega_fattura(request: Request, assegno_id: str, fattura_id: str = Body(..., embed=True)):
    verify_token(request)
    ass = await col_assegni().find_one({"$or": [{"_id": assegno_id}, {"numero": assegno_id}]})
    if not ass:
        raise HTTPException(status_code=404, detail="Assegno non trovato")

    fatt = await col_invoices().find_one({"_id": fattura_id})
    if not fatt:
        raise HTTPException(status_code=404, detail="Fattura non trovata")

    await col_assegni().update_one(
        {"_id": ass["_id"]},
        {"$set": {
            "fattura_collegata": fattura_id,
            "fornitore_piva":    fatt.get("fornitore_piva", ""),
            "beneficiario":      (fatt.get("fornitore_nome") or "")[:100],
            "importo":           fatt.get("importo_totale"),
            "numero_fattura":    fatt.get("numero_fattura", ""),
            "causale":           f"Fattura {fatt.get('numero_fattura')} del {fatt.get('data_fattura')}",
            "stato":             "compilato" if ass.get("stato") == "vuoto" else ass.get("stato"),
            "updated_at":        datetime.now(timezone.utc).isoformat(),
        }}
    )
    return {"ok": True}


# ── POST /api/assegni/{id}/emetti ─────────────────────────────────────────────
@router.post("/{assegno_id}/emetti")
async def emetti_assegno(
    request:       Request,
    assegno_id:    str,
    data_emissione: Optional[str] = Body(None),
):
    """Emette l'assegno (→ prima nota banca uscita)."""
    verify_token(request)

    ass = await col_assegni().find_one({"$or": [{"_id": assegno_id}, {"numero": assegno_id}]})
    if not ass:
        raise HTTPException(status_code=404, detail="Assegno non trovato")
    if ass.get("stato") == "vuoto":
        raise HTTPException(status_code=400, detail="Compilare l'assegno prima di emetterlo")

    if not data_emissione:
        data_emissione = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await col_assegni().update_one(
        {"_id": ass["_id"]},
        {"$set": {
            "stato":          "emesso",
            "data_emissione": data_emissione,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }}
    )

    # Crea movimento prima nota banca
    if ass.get("importo"):
        parti = [f"Assegno n.{ass.get('numero', assegno_id)}"]
        if ass.get("numero_fattura"):
            parti.append(f"Fatt.{ass['numero_fattura']}")
        if ass.get("beneficiario"):
            parti.append(ass["beneficiario"][:30])

        mov_id = new_id()
        mov = {
            "_id":             mov_id,
            "tipo":            "uscita",
            "importo":         float(ass["importo"]),
            "data":            data_emissione,
            "descrizione":     " – ".join(parti),
            "categoria":       "Assegni",
            "source":          "assegno_emesso",
            "assegno_id":      str(ass["_id"]),
            "assegno_numero":  ass.get("numero"),
            "fattura_id":      ass.get("fattura_collegata"),
            "fornitore_piva":  ass.get("fornitore_piva"),
            "riconciliato":    False,
            "anno":            int(data_emissione[:4]) if data_emissione and data_emissione[:4].isdigit() else datetime.now().year,
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }
        await col_pn_banca().insert_one(mov)
        await col_assegni().update_one(
            {"_id": ass["_id"]},
            {"$set": {"prima_nota_banca_id": mov_id}}
        )

    return {"ok": True, "stato": "emesso", "data_emissione": data_emissione}


# ── POST /api/assegni/{id}/incassa ────────────────────────────────────────────
@router.post("/{assegno_id}/incassa")
async def incassa_assegno(
    request:                  Request,
    assegno_id:               str,
    data_incasso:             Optional[str] = Body(None),
    movimento_estratto_id:    Optional[str] = Body(None),
):
    """Marca l'assegno come incassato e chiude la fattura collegata."""
    verify_token(request)
    db = get_db()

    ass = await col_assegni().find_one({"$or": [{"_id": assegno_id}, {"numero": assegno_id}]})
    if not ass:
        raise HTTPException(status_code=404, detail="Assegno non trovato")

    data_incasso = data_incasso or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    await col_assegni().update_one(
        {"_id": ass["_id"]},
        {"$set": {
            "stato":        "incassato",
            "data_incasso": data_incasso,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
        }}
    )

    # Riconcilia movimento prima nota banca
    if ass.get("prima_nota_banca_id"):
        await col_pn_banca().update_one(
            {"_id": ass["prima_nota_banca_id"]},
            {"$set": {
                "riconciliato": True,
                "data_riconciliazione": data_incasso,
                "movimento_estratto_id": movimento_estratto_id,
            }}
        )

    # Aggiorna fattura collegata → pagata
    fattura_chiusa = False
    if ass.get("fattura_collegata"):
        fid = ass["fattura_collegata"]
        await col_invoices().update_one(
            {"_id": fid},
            {"$set": {
                "stato":              "pagata",
                "data_pagamento":     data_incasso,
                "metodo_pagamento":   "assegno",
                "assegno_numero":     ass.get("numero"),
                "updated_at":        datetime.now(timezone.utc).isoformat(),
            }}
        )
        # Aggiorna scadenzario
        await db["scadenziario_fornitori"].update_many(
            {"fattura_id": fid, "pagato": {"$ne": True}},
            {"$set": {"pagato": True, "data_pagamento": data_incasso, "metodo_pagamento": "assegno"}}
        )
        fattura_chiusa = True

    # Riconcilia movimento estratto conto
    if movimento_estratto_id:
        await col_estratto_conto().update_one(
            {"_id": movimento_estratto_id},
            {"$set": {"riconciliato": True, "assegno_id": str(ass["_id"])}}
        )

    return {
        "ok":             True,
        "fattura_chiusa": fattura_chiusa,
        "data_incasso":   data_incasso,
    }


# ── POST /api/assegni/{id}/annulla ────────────────────────────────────────────
@router.post("/{assegno_id}/annulla")
async def annulla_assegno(request: Request, assegno_id: str):
    verify_token(request)
    r = await col_assegni().update_one(
        {"$or": [{"_id": assegno_id}, {"numero": assegno_id}]},
        {"$set": {"stato": "annullato", "updated_at": datetime.now(timezone.utc).isoformat()}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Assegno non trovato")
    return {"ok": True}


# ── DELETE /api/assegni/{id} ──────────────────────────────────────────────────
@router.delete("/{assegno_id}")
async def elimina_assegno(request: Request, assegno_id: str, force: bool = Query(False)):
    verify_token(request)
    doc = await col_assegni().find_one({"_id": assegno_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Assegno non trovato")

    if not force and doc.get("stato") in ("emesso", "incassato"):
        raise HTTPException(
            status_code=400,
            detail=f"Assegno '{doc.get('stato')}' non eliminabile. Usa force=true."
        )

    await col_assegni().update_one(
        {"_id": assegno_id},
        {"$set": {"entity_status": "deleted", "deleted_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"ok": True}


# ── POST /api/assegni/auto-match ──────────────────────────────────────────────
@router.post("/auto-match")
async def auto_match(request: Request, dry_run: bool = Query(False)):
    """Auto-match assegni ↔ fatture a 4 livelli (±0.005€, N:M)."""
    verify_token(request)
    from app.services.assegni_auto_match import run_auto_match
    db = get_db()
    report = await run_auto_match(db, dry_run=dry_run)
    return {
        "ok": True,
        **report,
        "riepilogo": {
            "L1": len(report.get("match_l1", [])),
            "L2": len(report.get("match_l2", [])),
            "L3": len(report.get("match_l3", [])),
            "L4": len(report.get("match_l4", [])),
            "ambigui":     len(report.get("ambigui", [])),
            "non_trovati": len(report.get("non_trovati", [])),
        },
    }


# ── POST /api/assegni/sync-da-estratto ───────────────────────────────────────
@router.post("/sync-da-estratto")
async def sync_da_estratto(request: Request):
    """
    Legge i movimenti 'PRELIEVO ASSEGNO' nell'estratto conto e:
     - se trova un assegno corrispondente nel carnet → lo marca incassato
     - altrimenti crea un assegno con stato=emesso (da riconciliare)
    """
    verify_token(request)

    PATTERNS = [
        r"NUM[:\s]+(\d{7,})",
        r"ASSEGNO\s*N\.?\s*(\d{7,})",
        r"PRELIEVO\s+ASSEGNO.*?(\d{7,})",
        r"VOSTRO\s+ASSEGNO\s*N?\.?\s*(\d{7,})",
        r"VS\.?\s*ASSEGNO\s*N?\.?\s*(\d{7,})",
        r"NUM[.:\s]*0*(\d{7,})",
        r"CRA[.:\s]*\d+\s+NUM[.:\s]*(\d{7,})",
    ]

    movimenti = await col_estratto_conto().find({
        "$or": [
            {"descrizione": {"$regex": "ASSEGNO", "$options": "i"}},
            {"descrizione": {"$regex": "PRELIEVO", "$options": "i"}},
        ],
        "tipo": "uscita",
    }).to_list(500)

    creati = 0
    riconciliati = 0
    saltati = 0

    for mov in movimenti:
        desc = mov.get("descrizione") or ""
        if "RILASCIO CARNET" in desc.upper():
            continue

        # Estrai numero assegno dalla descrizione
        numero_ass = None
        for pat in PATTERNS:
            m = re.search(pat, desc, re.IGNORECASE)
            if m:
                numero_ass = m.group(1)
                break

        if not numero_ass:
            numero_ass = f"AUTO-{str(mov.get('_id', ''))[:8]}"

        importo = abs(float(mov.get("importo", 0)))
        data    = mov.get("data") or mov.get("data_valuta") or datetime.now().strftime("%Y-%m-%d")

        # Cerca nel carnet assegni esistente
        carnet = await col_assegni().find_one({
            "$or": [
                {"numero": {"$regex": numero_ass[-8:] if len(numero_ass) >= 8 else numero_ass}},
                {"numero": numero_ass},
            ],
            "stato": {"$in": ["compilato", "emesso"]},
        })

        if carnet:
            # Marca incassato
            await col_assegni().update_one(
                {"_id": carnet["_id"]},
                {"$set": {
                    "stato":             "incassato",
                    "data_incasso":      data,
                    "movimento_estratto_id": str(mov.get("_id", "")),
                    "updated_at":        datetime.now(timezone.utc).isoformat(),
                }}
            )
            # Chiudi fattura
            if carnet.get("fattura_collegata"):
                await col_invoices().update_one(
                    {"_id": carnet["fattura_collegata"]},
                    {"$set": {"stato": "pagata", "data_pagamento": data, "metodo_pagamento": "assegno"}}
                )
            riconciliati += 1
            continue

        # Controlla se già creato da un sync precedente
        existing = await col_assegni().find_one({
            "$or": [
                {"numero": numero_ass},
                {"movimento_estratto_id": str(mov.get("_id", ""))},
            ]
        })
        if existing:
            saltati += 1
            continue

        # Crea assegno da estratto conto
        doc = {
            "_id":                  new_id(),
            "numero":               numero_ass,
            "importo":              importo,
            "data_emissione":       data,
            "stato":                "emesso",
            "beneficiario":         "",
            "descrizione":          desc[:80],
            "movimento_estratto_id": str(mov.get("_id", "")),
            "fonte":                "estratto_conto",
            "entity_status":        "active",
            "created_at":           datetime.now(timezone.utc).isoformat(),
            "updated_at":           datetime.now(timezone.utc).isoformat(),
        }
        await col_assegni().insert_one(doc)
        creati += 1

    return {
        "ok":           True,
        "analizzati":   len(movimenti),
        "riconciliati": riconciliati,
        "creati":       creati,
        "saltati":      saltati,
    }
