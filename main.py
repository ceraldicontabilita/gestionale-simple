"""
Ceraldi Group ERP — FastAPI Backend
Entry point principale. Tutti i router sono registrati qui.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# ── Router imports ────────────────────────────────────────────────────────────
from app.routers.auth          import router as auth_router
from app.routers.pec           import router as pec_router
from app.routers.fatture       import router as fatture_router
from app.routers.prima_nota    import router as prima_nota_router
from app.routers.fornitori     import router as fornitori_router
from app.routers.corrispettivi import router as corrispettivi_router
from app.routers.dipendenti    import router as dipendenti_router
from app.routers.cedolini      import router as cedolini_router
from app.routers.verbali       import router as verbali_router
from app.routers.veicoli       import router as veicoli_router
from app.routers.estratto_conto import router as estratto_router
from app.routers.scadenzario   import router as scadenzario_router


# ── Startup / Shutdown ────────────────────────────────────────────────────────
async def _cleanup_fornitori_orphan():
    """Rimuove fornitori senza P.IVA e senza ragione_sociale (creati per errore da XML vuoto)."""
    try:
        from app.database import get_db
        db = get_db()
        result = await db["fornitori"].delete_many({
            "$and": [
                {"$or": [{"ragione_sociale": {"$in": [None, ""]}}, {"ragione_sociale": {"$exists": False}}]},
                {"$or": [{"partita_iva":     {"$in": [None, ""]}}, {"partita_iva":     {"$exists": False}}]},
            ]
        })
        if result.deleted_count:
            print(f"✅ Cleanup: rimossi {result.deleted_count} fornitori orphan senza P.IVA/nome")
    except Exception as e:
        print(f"⚠️  Cleanup fornitori orphan: {e}")


async def _migra_nomi_fornitori():
    """Popola fornitore_nome sui documenti che ce l'hanno vuoto, usando l'anagrafica."""
    try:
        from app.database import get_db
        db = get_db()
        all_forn = await db["fornitori"].find(
            {"ragione_sociale": {"$exists": True, "$ne": ""}},
            {"partita_iva": 1, "ragione_sociale": 1}
        ).to_list(length=None)
        forn_map = {
            f["partita_iva"]: f["ragione_sociale"]
            for f in all_forn if f.get("partita_iva") and f.get("ragione_sociale")
        }
        if not forn_map:
            return
        cursor = db["invoices"].find(
            {"$or": [{"fornitore_nome": ""}, {"fornitore_nome": None},
                     {"fornitore_nome": {"$exists": False}}]},
            {"_id": 1, "fornitore_piva": 1}
        )
        docs = await cursor.to_list(length=None)
        aggiornate = 0
        for d in docs:
            nome = forn_map.get(d.get("fornitore_piva", ""), "")
            if nome:
                await db["invoices"].update_one(
                    {"_id": d["_id"]}, {"$set": {"fornitore_nome": nome}}
                )
                aggiornate += 1
        if aggiornate:
            print(f"✅ Migration: aggiornati {aggiornate} fornitore_nome")
    except Exception as e:
        print(f"⚠️  Migration nomi fornitori: {e}")


async def _migra_corrispettivi():
    """Imposta contanti=0 e elettronico=0 sui corrispettivi che non hanno questi campi."""
    try:
        from app.database import get_db
        db = get_db()
        result = await db["corrispettivi"].update_many(
            {"$or": [{"contanti": {"$exists": False}}, {"contanti": None}]},
            {"$set": {"contanti": 0.0, "elettronico": 0.0}},
        )
        if result.modified_count:
            print(f"✅ Migration corrispettivi: aggiornati {result.modified_count} record (contanti/elettronico → 0)")
    except Exception as e:
        print(f"⚠️  Migration corrispettivi: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    try:
        from app.database import get_client
        await get_client().admin.command("ping")
        print("✅ MongoDB Atlas connesso")
        asyncio.create_task(_cleanup_fornitori_orphan())
        asyncio.create_task(_migra_nomi_fornitori())
        asyncio.create_task(_migra_corrispettivi())
    except Exception as e:
        print(f"⚠️  MongoDB non raggiungibile: {e}")
    yield
    from app.database import _client
    if _client:
        _client.close()
        print("🔌 MongoDB disconnesso")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Ceraldi Group ERP",
    version="2.0.0",
    description="Gestionale contabile — Ceraldi Group SRL, Napoli",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

IS_RENDER = os.getenv("RENDER", "false").lower() == "true"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not IS_RENDER else [
        "https://gestionale-ceraldi.onrender.com",
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Registra router ───────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(pec_router)
app.include_router(fatture_router)
app.include_router(prima_nota_router)
app.include_router(fornitori_router)
app.include_router(corrispettivi_router)
app.include_router(dipendenti_router)
app.include_router(cedolini_router)
app.include_router(verbali_router)
app.include_router(veicoli_router)
app.include_router(estratto_router)
app.include_router(scadenzario_router)


# ── Dashboard summary ────────────────────────────────────────────────────────
@app.get("/api/dashboard/summary")
async def dashboard_summary(anno: int = None):
    """KPI aggregati per la dashboard principale."""
    import datetime
    if anno is None:
        anno = datetime.datetime.now().year
    try:
        from app.database import get_db
        db = get_db()
        # Conti rapidi
        fatture_da_pagare = await db["invoices"].count_documents({"stato": "da_pagare", "anno": anno})
        provvisori        = await db["prima_nota_provvisori"].count_documents({"stato": "provvisorio"})
        verbali_da_pagare = await db["verbali_noleggio"].count_documents({"stato": "da_pagare"})
        f24_da_pagare     = await db["f24_commercialista"].count_documents({"stato": "da_pagare"})

        # Saldo cassa (ultimo disponibile)
        cassa_pipe = [{"$group": {"_id": "$tipo", "tot": {"$sum": "$importo"}}}]
        cassa_docs = await db["prima_nota_cassa"].aggregate(cassa_pipe).to_list(length=10)
        saldo_cassa = sum(d["tot"] if d["_id"] == "entrata" else -d["tot"] for d in cassa_docs)

        # Saldo banca
        banca_docs = await db["prima_nota_banca"].aggregate(cassa_pipe).to_list(length=10)
        saldo_banca = sum(d["tot"] if d["_id"] == "entrata" else -d["tot"] for d in banca_docs)

        return {
            "anno": anno,
            "fatture_da_pagare":  fatture_da_pagare,
            "provvisori":         provvisori,
            "verbali_da_pagare":  verbali_da_pagare,
            "f24_da_pagare":      f24_da_pagare,
            "saldo_cassa":        round(saldo_cassa, 2),
            "saldo_banca":        round(saldo_banca, 2),
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"errore": str(e)})


@app.get("/api/commercialista/alert-status")
async def commercialista_alert():
    """Controlla se ci sono mesi da inviare al commercialista."""
    import datetime
    now = datetime.datetime.now()
    # Avvisa il 10 del mese successivo
    show = now.day >= 10
    mese_pendente = now.month - 1 if now.month > 1 else 12
    anno_pendente = now.year if now.month > 1 else now.year - 1
    return {
        "show_alert":    show,
        "mese_pendente": mese_pendente,
        "anno_pendente": anno_pendente,
        "message":       f"Ricorda di inviare i dati di {mese_pendente}/{anno_pendente} al commercialista",
    }


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0", "app": "Ceraldi ERP"}


@app.get("/api/status")
async def api_status():
    """Controlla connessione MongoDB e restituisce statistiche base."""
    try:
        from app.database import get_client, get_db, DB_NAME
        await get_client().admin.command("ping")
        db = get_db()
        stats = {}
        for coll_name, label in {
            "invoices":              "fatture",
            "fornitori":             "fornitori",
            "prima_nota_cassa":      "mov_cassa",
            "prima_nota_banca":      "mov_banca",
            "prima_nota_provvisori": "provvisori",
            "corrispettivi":         "corrispettivi",
            "dipendenti":            "dipendenti",
            "cedolini":              "cedolini",
            "f24":                   "f24",
            "documents_inbox":       "doc_inbox",
            "verbali_noleggio":      "verbali",
            "veicoli":               "veicoli",
            "estratto_conto":        "estratto_conto",
        }.items():
            try:
                stats[label] = await db[coll_name].count_documents({})
            except Exception:
                stats[label] = -1
        return {"status": "ok", "database": DB_NAME, "collections": stats}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


# ── Serve frontend HTML ───────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", include_in_schema=False)
async def serve_index():
    index = Path(__file__).parent / "static" / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"detail": "Frontend non trovato"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
