"""
PEC Aruba IMAP Service — Ceraldi Group ERP
Scarica email con allegati XML/P7M dalla casella PEC Aruba.
Gestisce INBOX e INBOX.lette. Deduplicazione via xml_hash MD5.
"""
import os
import email
import imaplib
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

from app.database import col_invoices, col_fornitori, col_pec_inbox
from app.services.xml_parser import parse_file
from app.services.prima_nota_auto import smista_fattura

load_dotenv()

PEC_HOST     = os.getenv("PEC_HOST", "imaps.pec.aruba.it")
PEC_PORT     = int(os.getenv("PEC_PORT", "993"))
PEC_EMAIL    = os.getenv("PEC_EMAIL", "")
PEC_PASSWORD = os.getenv("PEC_PASSWORD", "")

# Mittenti SDI attendibili
SDI_SENDERS = {
    "pec.fatturapa.gov.it",
    "agenziaentrate.gov.it",
    "sdi.fatturapa.gov.it",
    "pec.fatturapa.it",
    "noreply@pec.fatturapa.gov.it",
}

CARTELLE_PEC = ["INBOX", "INBOX.lette"]


def _is_sdi(from_addr: str) -> bool:
    from_addr = from_addr.lower()
    return any(s in from_addr for s in SDI_SENDERS)


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


async def sync_pec(giorni_indietro: int = 7, forza_riprocessa: bool = False) -> dict:
    """
    Connette alla PEC, scarica email non processate, parsa le fatture.
    Ritorna: {nuove: int, duplicate: int, errori: int, dettagli: list}
    """
    stats = {"nuove": 0, "duplicate": 0, "errori": 0, "dettagli": []}

    try:
        conn = imaplib.IMAP4_SSL(PEC_HOST, PEC_PORT)
        conn.login(PEC_EMAIL, PEC_PASSWORD)
    except Exception as e:
        raise ConnectionError(f"Connessione PEC fallita: {e}")

    try:
        for cartella in CARTELLE_PEC:
            try:
                status, _ = conn.select(cartella)
                if status != "OK":
                    continue
            except Exception:
                continue

            # Ricerca per data (ultimi N giorni)
            since_date = (datetime.now() - timedelta(days=giorni_indietro)).strftime("%d-%b-%Y")
            _, msg_nums = conn.search(None, f'(SINCE "{since_date}")')
            if not msg_nums or not msg_nums[0]:
                continue

            for num in msg_nums[0].split():
                try:
                    _, msg_data = conn.fetch(num, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    from_addr = _decode_header(msg.get("From", ""))
                    subject   = _decode_header(msg.get("Subject", ""))
                    msg_id    = msg.get("Message-ID", "")

                    if not _is_sdi(from_addr):
                        continue

                    # Controlla se già processata
                    if not forza_riprocessa:
                        existing = await col_pec_inbox().find_one({"message_id": msg_id})
                        if existing and existing.get("processato"):
                            stats["duplicate"] += 1
                            continue

                    # Estrai allegati XML/P7M
                    allegati = []
                    for part in msg.walk():
                        fname = part.get_filename("")
                        if not fname:
                            continue
                        fname = _decode_header(fname)
                        if fname.lower().endswith((".xml", ".p7m", ".xml.p7m")):
                            allegati.append({
                                "nome": fname,
                                "contenuto": part.get_payload(decode=True),
                            })

                    if not allegati:
                        continue

                    # Salva raw in pec_inbox
                    pec_doc = {
                        "message_id": msg_id,
                        "da": from_addr,
                        "oggetto": subject,
                        "data_ricezione": datetime.utcnow().isoformat(),
                        "processato": False,
                        "fattura_id": None,
                        "errore_parsing": None,
                    }
                    await col_pec_inbox().update_one(
                        {"message_id": msg_id},
                        {"$setOnInsert": pec_doc},
                        upsert=True,
                    )

                    # Processa ogni allegato
                    for allegato in allegati:
                        result = await _processa_allegato(
                            allegato["nome"],
                            allegato["contenuto"],
                            msg_id,
                        )
                        stats["dettagli"].append(result)
                        if result["esito"] == "nuova":
                            stats["nuove"] += 1
                        elif result["esito"] == "duplicata":
                            stats["duplicate"] += 1
                        else:
                            stats["errori"] += 1

                except Exception as e:
                    stats["errori"] += 1
                    stats["dettagli"].append({"esito": "errore", "messaggio": str(e)})

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return stats


async def _processa_allegato(filename: str, data: bytes, msg_id: str) -> dict:
    """Parsa un allegato, controlla duplicati, inserisce in DB, smista prima nota."""
    try:
        fattura = parse_file(filename, data)
    except Exception as e:
        await col_pec_inbox().update_one(
            {"message_id": msg_id},
            {"$set": {"errore_parsing": str(e)}}
        )
        return {"esito": "errore", "messaggio": str(e), "file": filename}

    xml_hash = fattura["xml_hash"]

    # Dedup: stesso hash = stessa fattura
    existing = await col_invoices().find_one({"xml_hash": xml_hash})
    if existing:
        return {
            "esito": "duplicata",
            "fattura_id": str(existing["_id"]),
            "numero": fattura["numero_fattura"],
        }

    # Crea/aggiorna fornitore in anagrafica
    await _upsert_fornitore(fattura)

    # Recupera metodo pagamento del fornitore
    fornitore = await col_fornitori().find_one(
        {"partita_iva": fattura["fornitore_piva"]}
    )
    metodo = fornitore.get("metodo_pagamento", "") if fornitore else ""
    fattura["metodo_pagamento"] = metodo
    fattura["source"] = "pec"

    # Inserisce fattura
    result = await col_invoices().insert_one(fattura)
    fattura_id = str(result.inserted_id)

    # Aggiorna pec_inbox con riferimento
    await col_pec_inbox().update_one(
        {"message_id": msg_id},
        {"$set": {"processato": True, "fattura_id": fattura_id}}
    )

    # Smista in prima nota automaticamente
    smistamento = await smista_fattura(fattura_id, fattura, metodo)

    return {
        "esito": "nuova",
        "fattura_id": fattura_id,
        "numero": fattura["numero_fattura"],
        "fornitore": fattura["fornitore_nome"],
        "importo": fattura["importo_totale"],
        "prima_nota": smistamento,
    }


async def processa_singola_pec(pec_id: str) -> dict:
    """Riprocessa una singola email PEC già salvata in pec_inbox."""
    doc = await col_pec_inbox().find_one({"_id": pec_id})
    if not doc:
        raise ValueError(f"Email PEC {pec_id} non trovata")

    # Bisogna ri-scaricare gli allegati — per ora usiamo il raw se salvato
    # In produzione questo endpoint serve per riprocessare errori
    return {"messaggio": "Riprocessamento non supportato senza allegato raw salvato"}


async def _upsert_fornitore(fattura: dict):
    """Crea il fornitore se non esiste, aggiorna IBAN se nuovo."""
    piva = fattura["fornitore_piva"]
    if not piva:
        return

    update_data = {
        "ragione_sociale": fattura["fornitore_nome"],
        "codice_fiscale":  fattura["fornitore_cf"],
        "indirizzo":       fattura["fornitore_indirizzo"],
        "updated_at":      datetime.utcnow().isoformat(),
    }
    if fattura.get("fornitore_iban"):
        update_data["iban"] = fattura["fornitore_iban"]

    await col_fornitori().update_one(
        {"partita_iva": piva},
        {
            "$setOnInsert": {
                "partita_iva":       piva,
                "metodo_pagamento":  "",
                "categoria_iva":     "",
                "detraibilita_default_pct": 100.0,
                "ateco_code":        "",
                "created_at":        datetime.utcnow().isoformat(),
            },
            "$set": update_data,
        },
        upsert=True,
    )
