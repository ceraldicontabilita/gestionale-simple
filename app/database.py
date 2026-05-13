"""
Connessione MongoDB Atlas — Ceraldi Group ERP
Tutte le collection usano i nomi del DB esistente (non rinominarle).
"""
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "")
DB_NAME   = os.getenv("DB_NAME", "Gestionale")

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    return _client


def get_db():
    return get_client()[DB_NAME]


# ── Shorthand per ogni collection (nomi del DB reale) ────────────────────────

def col_invoices():
    return get_db()["invoices"]

def col_fornitori():
    return get_db()["fornitori"]

def col_pn_cassa():
    return get_db()["prima_nota_cassa"]

def col_pn_banca():
    return get_db()["prima_nota_banca"]

def col_pn_provvisori():
    return get_db()["prima_nota_provvisori"]

def col_corrispettivi():
    return get_db()["corrispettivi"]

def col_estratto_conto():
    return get_db()["estratto_conto"]

def col_assegni():
    return get_db()["assegni"]

def col_dipendenti():
    return get_db()["dipendenti"]

def col_cedolini():
    return get_db()["cedolini"]

def col_presenze():
    return get_db()["presenze"]

def col_verbali():
    return get_db()["verbali_noleggio"]

def col_veicoli():
    return get_db()["veicoli_noleggio"]

def col_f24():
    return get_db()["f24_commercialista"]

def col_f24_attachments():
    return get_db()["f24_email_attachments"]

def col_warehouse():
    return get_db()["warehouse_stocks"]

def col_documents_inbox():
    return get_db()["documents_inbox"]

def col_documenti_non_associati():
    return get_db()["documenti_non_associati"]

def col_scadenzario():
    return get_db()["scadenziario_fornitori"]

def col_pec_inbox():
    return get_db()["pec_inbox"]
