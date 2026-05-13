"""
Router Scadenzario — Ceraldi Group ERP
Vista unificata di TUTTE le scadenze imminenti:
  - Fatture fornitori da pagare
  - F24 paghe
  - Verbali/multe
  - Bollo/revisione/assicurazione veicoli
  - Fine contratto noleggio
  - Corrispettivi con discrepanza POS non risolta
"""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Request, Query

from app.routers.auth import verify_token
from app.database import get_db

router = APIRouter(prefix="/api/scadenzario", tags=["scadenzario"])


def _ser(doc):
    if not doc:
        return {}
    from bson import ObjectId
    return {k: str(v) if isinstance(v, ObjectId) else v for k, v in doc.items()}


@router.get("")
async def scadenzario_unificato(
    request: Request,
    giorni:  int          = Query(30),
    tipo:    Optional[str] = Query(None),  # fatture|f24|verbali|veicoli|corrispettivi
):
    """
    Restituisce tutte le scadenze entro N giorni, ordinate per data.
    Ogni item ha: tipo, descrizione, importo, scadenza, giorni_mancanti, id, urgente (<=7gg).
    """
    verify_token(request)
    db    = get_db()
    oggi  = date.today()
    entro = (oggi + timedelta(days=giorni)).isoformat()
    oggi_s = oggi.isoformat()

    items = []

    # ── 1. Fatture da pagare ──────────────────────────────────────────────────
    if not tipo or tipo == "fatture":
        cursor = db["invoices"].find({
            "stato":            {"$in": ["da_pagare", "scaduta"]},
            "data_scadenza":    {"$gte": oggi_s, "$lte": entro},
            "deleted_at":       {"$exists": False},
        }).sort("data_scadenza", 1).limit(200)
        docs = await cursor.to_list(length=200)
        for d in docs:
            scad = d.get("data_scadenza", "")
            try:
                dt = date.fromisoformat(scad[:10])
                gg = (dt - oggi).days
            except Exception:
                gg = 0
            items.append({
                "tipo":        "fattura",
                "id":          str(d.get("_id", "")),
                "descrizione": f"Fattura {d.get('numero_fattura','')} — {d.get('fornitore_nome','')}",
                "importo":     d.get("importo_totale", 0.0),
                "scadenza":    scad[:10] if scad else "",
                "giorni":      gg,
                "urgente":     gg <= 7,
                "link":        f"/fatture/{d.get('_id','')}",
            })

    # ── 2. F24 da pagare ──────────────────────────────────────────────────────
    if not tipo or tipo == "f24":
        cursor = db["f24_commercialista"].find({
            "stato":    "da_pagare",
            "scadenza": {"$gte": oggi_s, "$lte": entro},
        }).sort("scadenza", 1).limit(50)
        docs = await cursor.to_list(length=50)
        for d in docs:
            scad = d.get("scadenza", "")
            try:
                gg = (date.fromisoformat(scad[:10]) - oggi).days
            except Exception:
                gg = 0
            items.append({
                "tipo":        "f24",
                "id":          str(d.get("_id", "")),
                "descrizione": f"F24 paghe {d.get('periodo','')} scad. {scad[:10]}",
                "importo":     d.get("totale", 0.0),
                "scadenza":    scad[:10],
                "giorni":      gg,
                "urgente":     gg <= 7,
                "link":        f"/cedolini/f24",
            })

    # ── 3. Verbali da pagare ──────────────────────────────────────────────────
    if not tipo or tipo == "verbali":
        cursor = db["verbali_noleggio"].find({
            "stato":              "da_pagare",
            "scadenza_pagamento": {"$gte": oggi_s, "$lte": entro},
        }).sort("scadenza_pagamento", 1).limit(100)
        docs = await cursor.to_list(length=100)
        for d in docs:
            scad = d.get("scadenza_pagamento", "")
            try:
                gg = (date.fromisoformat(scad[:10]) - oggi).days
            except Exception:
                gg = 0
            items.append({
                "tipo":        "verbale",
                "id":          str(d.get("_id", "")),
                "descrizione": f"Multa {d.get('numero_verbale','')} — {d.get('targa','')} — {d.get('ente_emittente','')}",
                "importo":     d.get("importo_da_pagare", 0.0),
                "scadenza":    scad[:10],
                "giorni":      gg,
                "urgente":     gg <= 7,
                "link":        f"/verbali",
            })

    # ── 4. Scadenze veicoli ───────────────────────────────────────────────────
    if not tipo or tipo == "veicoli":
        cursor = db["veicoli_noleggio"].find({"attivo": True})
        docs   = await cursor.to_list(length=200)
        for d in docs:
            for campo, label in [
                ("scadenza_bollo",           "Bollo"),
                ("scadenza_revisione",       "Revisione"),
                ("scadenza_assicurazione",   "Assicurazione"),
                ("fine_contratto_noleggio",  "Fine noleggio"),
            ]:
                scad = d.get(campo, "")
                if not scad:
                    continue
                try:
                    dt = date.fromisoformat(scad[:10])
                    gg = (dt - oggi).days
                except Exception:
                    continue
                if 0 <= gg <= giorni:
                    items.append({
                        "tipo":        "veicolo",
                        "id":          str(d.get("_id", "")),
                        "descrizione": f"{label} — {d.get('targa','')} {d.get('marca','')} {d.get('modello','')}",
                        "importo":     d.get("canone_mensile", 0.0) if campo == "fine_contratto_noleggio" else 0.0,
                        "scadenza":    scad[:10],
                        "giorni":      gg,
                        "urgente":     gg <= 7,
                        "link":        f"/veicoli",
                    })

    # ── 5. Corrispettivi con discrepanza POS non risolta ──────────────────────
    if not tipo or tipo == "corrispettivi":
        cursor = db["corrispettivi"].find({
            "alert_discrepanza": True,
            "data":              {"$gte": (oggi - timedelta(days=giorni)).isoformat()},
        }).sort("data", -1).limit(50)
        docs = await cursor.to_list(length=50)
        for d in docs:
            items.append({
                "tipo":        "corrispettivo",
                "id":          str(d.get("_id", "")),
                "descrizione": f"Discrepanza POS — {d.get('data','')} — diff €{abs(d.get('discrepanza',0)):.2f}",
                "importo":     abs(d.get("discrepanza", 0.0)),
                "scadenza":    d.get("data", ""),
                "giorni":      0,
                "urgente":     True,
                "link":        f"/corrispettivi",
            })

    # Ordina per giorni mancanti
    items.sort(key=lambda x: (x["giorni"] if x["giorni"] >= 0 else 999))

    totale_importo = sum(i["importo"] for i in items if i["tipo"] != "veicolo")
    urgenti        = [i for i in items if i["urgente"]]

    return {
        "scadenze":      items,
        "totale":        len(items),
        "urgenti":       len(urgenti),
        "totale_importo": round(totale_importo, 2),
        "entro_giorni":  giorni,
    }


# ── GET /api/scadenzario/oggi ─────────────────────────────────────────────────
@router.get("/oggi")
async def scadenze_oggi(request: Request):
    """Scadenze di oggi + scadute non pagate (giorni=0, include arretrati)."""
    verify_token(request)
    db    = get_db()
    oggi  = date.today().isoformat()
    items = []

    # Fatture scadute o in scadenza oggi
    cursor = db["invoices"].find({
        "stato":      {"$in": ["da_pagare", "scaduta"]},
        "data_scadenza": {"$lte": oggi},
        "deleted_at": {"$exists": False},
    }).sort("data_scadenza", 1).limit(100)
    docs = await cursor.to_list(length=100)
    for d in docs:
        scad = d.get("data_scadenza", "")
        try:
            gg = (date.fromisoformat(scad[:10]) - date.today()).days
        except Exception:
            gg = 0
        items.append({
            "tipo":        "fattura",
            "id":          str(d.get("_id", "")),
            "descrizione": f"Fattura {d.get('numero_fattura','')} — {d.get('fornitore_nome','')}",
            "importo":     d.get("importo_totale", 0.0),
            "scadenza":    scad[:10] if scad else "",
            "giorni":      gg,
            "urgente":     True,
        })

    # F24 scaduti o in scadenza oggi
    cursor2 = db["f24_commercialista"].find({
        "stato":    "da_pagare",
        "scadenza": {"$lte": oggi},
    }).sort("scadenza", 1).limit(20)
    docs2 = await cursor2.to_list(length=20)
    for d in docs2:
        scad = d.get("scadenza", "")
        try:
            gg = (date.fromisoformat(scad[:10]) - date.today()).days
        except Exception:
            gg = 0
        items.append({
            "tipo":        "f24",
            "id":          str(d.get("_id", "")),
            "descrizione": f"F24 {d.get('periodo','')} — scad. {scad[:10]}",
            "importo":     d.get("totale", 0.0),
            "scadenza":    scad[:10],
            "giorni":      gg,
            "urgente":     True,
        })

    items.sort(key=lambda x: x["giorni"])
    return {
        "scadenze_oggi_arretrate": items,
        "totale":   len(items),
        "importo":  round(sum(i["importo"] for i in items), 2),
    }
