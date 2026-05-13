"""
Gmail Scanner — Ceraldi Group ERP
Scansiona Gmail (IMAP) alla ricerca di allegati PDF da mittenti attendibili.
Gestisce: Libro Unico, F24 paghe, CU, cedolini Zucchetti.
"""
import imaplib
import email
import os
import hashlib
import base64
from datetime import datetime, timedelta
from email.header import decode_header
from typing import Optional

from app.database import get_db

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── Mittenti attendibili ───────────────────────────────────────────────────────
TRUSTED_SENDERS = {
    # Studio Ferrantini (paghe / cedolini / F24 / CU)
    "f.ferrantini@email.it":             "paghe",
    "grazia.studioferrantini@email.it":  "paghe",

    # Commercialista / contabilità
    "rosaria.marotta@email.it":          "contabilita",
    "studio@marotta.it":                 "contabilita",

    # Banche
    "estrattoconto@unicredit.it":        "banca",
    "noreply@bn.it":                     "banca",
    "estrattoconto@intesasanpaolo.com":  "banca",

    # Aruba PEC / fatture
    "notifiche@pec.aruba.it":            "pec",
    "sdi@pec.fatturapa.it":              "sdi",
    "noreply@fatturapa.gov.it":          "sdi",

    # Assicurazioni
    "polizze@generali.it":               "assicurazione",
    "info@allianz.it":                   "assicurazione",

    # Noleggio veicoli
    "fatture@leasys.com":                "noleggio",
    "billing@aldautomotive.com":         "noleggio",

    # INAIL / INPS / AdE
    "noreply@inail.it":                  "enti",
    "noreply@agenziaentrate.gov.it":     "enti",
    "avvisi@inps.it":                    "enti",
}

# Keyword → tipo documento
PDF_CLASSIFIERS = {
    "libro unico":   "libro_unico",
    "librouni":      "libro_unico",
    "cedolino":      "cedolino",
    "payslip":       "cedolino",
    "f24":           "f24",
    "modello f24":   "f24",
    "riepilogo paghe": "riepilogo_paghe",
    "riepilogo_paghe": "riepilogo_paghe",
    "cu ":           "cu",
    "certif":        "cu",
    "estratto":      "estratto_conto",
    "movimenti":     "estratto_conto",
    "polizza":       "assicurazione",
    "verbale":       "verbale",
    "noleggio":      "noleggio",
}


def _decode_str(s) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _classifica_pdf(filename: str, subject: str) -> str:
    """Restituisce il tipo documento dal nome file o dall'oggetto email."""
    testo = (filename + " " + subject).lower()
    for kw, tipo in PDF_CLASSIFIERS.items():
        if kw in testo:
            return tipo
    return "generico"


def _mittente_normalizzato(from_str: str) -> Optional[str]:
    """Estrae l'indirizzo email dal campo From."""
    from_str = _decode_str(from_str).lower()
    import re
    match = re.search(r"<([^>]+)>", from_str)
    if match:
        return match.group(1).strip()
    # Solo indirizzo senza <>
    match = re.search(r"[\w.+-]+@[\w.-]+", from_str)
    if match:
        return match.group(0).strip()
    return None


async def _salva_documento(
    filename:      str,
    pdf_bytes:     bytes,
    tipo:          str,
    mittente:      str,
    categoria_mit: str,
    subject:       str,
    data_email:    str,
):
    """Inserisce il PDF nel documento inbox (dedup su MD5)."""
    db = get_db()
    doc_hash = hashlib.md5(pdf_bytes).hexdigest()

    existing = await db["documents_inbox"].find_one({"file_hash": doc_hash})
    if existing:
        return {"skip": True, "id": str(existing["_id"]), "motivo": "duplicato"}

    doc = {
        "file_hash":      doc_hash,
        "filename":       filename,
        "tipo":           tipo,
        "categoria":      categoria_mit,
        "mittente":       mittente,
        "subject":        subject,
        "data_email":     data_email,
        "pdf_bytes_b64":  base64.b64encode(pdf_bytes).decode(),
        "processato":     False,
        "risultati":      {},
        "created_at":     datetime.utcnow().isoformat(),
    }
    r = await db["documents_inbox"].insert_one(doc)

    # Auto-processa i tipi noti
    result_proc = await _auto_processa(str(r.inserted_id), tipo, pdf_bytes)
    return {"ok": True, "id": str(r.inserted_id), "tipo": tipo, "processato": result_proc}


async def _auto_processa(doc_id: str, tipo: str, pdf_bytes: bytes) -> dict:
    """Processa automaticamente i tipi noti."""
    db = get_db()
    risultati = {}

    try:
        if tipo == "f24":
            from app.services.ai_parser import parse_f24
            dati = await parse_f24(pdf_bytes)
            risultati = await _inserisci_f24(dati, doc_id)

        elif tipo in ("libro_unico", "cedolino", "riepilogo_paghe"):
            from app.services.ai_parser import parse_cedolino
            dati = await parse_cedolino(pdf_bytes)
            risultati = await _inserisci_cedolini(dati, doc_id)

        elif tipo == "estratto_conto":
            # Solo salvataggio — la riconciliazione è manuale o via /confronto-pos
            risultati = {"note": "estratto_conto salvato — riconciliazione manuale"}

    except Exception as e:
        risultati = {"errore": str(e)}

    await db["documents_inbox"].update_one(
        {"_id": doc_id},
        {"$set": {"processato": True, "risultati": risultati}}
    )
    return risultati


async def _inserisci_f24(dati: dict, doc_id: str) -> dict:
    """
    Inserisce F24 in col_f24 e crea movimento provvisorio in prima_nota_banca.
    Il movimento diventerà RICONCILIATO quando trovo il pagamento in estratto conto.
    """
    db = get_db()
    import uuid

    scadenza = dati.get("scadenza", "")
    totale   = dati.get("totale_saldo", 0.0)
    periodo  = dati.get("periodo", "")
    codici   = dati.get("codici_tributo", [])

    # Salva F24 strutturato
    f24_doc = {
        "_id":         str(uuid.uuid4()),
        "doc_inbox_id": doc_id,
        "scadenza":    scadenza,
        "periodo":     periodo,
        "totale":      totale,
        "codici_tributo": codici,
        "stato":       "da_pagare",
        "created_at":  datetime.utcnow().isoformat(),
    }
    await db["f24"].insert_one(f24_doc)

    # Crea movimento provvisorio in prima nota banca (USCITA)
    if totale > 0:
        descrizione_codici = ", ".join(
            f"{c.get('codice_tributo','')} {c.get('descrizione','')} €{c.get('importo_debito',0):.2f}"
            for c in codici if c.get("importo_debito", 0) > 0
        )

        prov_doc = {
            "_id":          str(uuid.uuid4()),
            "tipo":         "uscita",
            "importo":      totale,
            "data":         scadenza or datetime.utcnow().strftime("%Y-%m-%d"),
            "descrizione":  f"F24 paghe scad. {scadenza} | {descrizione_codici}",
            "categoria":    "f24_paghe",
            "f24_id":       f24_doc["_id"],
            "doc_inbox_id": doc_id,
            "stato":        "provvisorio",
            "codici_tributo_cerca": [c.get("codice_tributo") for c in codici],
            "created_at":   datetime.utcnow().isoformat(),
        }
        await db["prima_nota_provvisori"].insert_one(prov_doc)

    return {
        "f24_id":      f24_doc["_id"],
        "totale":      totale,
        "provvisorio": totale > 0,
        "codici":      len(codici),
    }


async def _inserisci_cedolini(dati: dict, doc_id: str) -> dict:
    """Upsert dipendenti e cedolini dal Libro Unico."""
    db = get_db()
    import uuid

    dipendenti_list = dati.get("dipendenti", [])
    contatori = {"upsert_dipendenti": 0, "cedolini_inseriti": 0, "skip": 0}

    for dip in dipendenti_list:
        cf = dip.get("codice_fiscale", "").upper().strip()
        if not cf:
            continue

        # Upsert dipendente
        await db["dipendenti"].update_one(
            {"codice_fiscale": cf},
            {"$set": {
                "nome":            dip.get("nome", ""),
                "cognome":         dip.get("cognome", ""),
                "iban":            dip.get("iban", ""),
                "qualifica":       dip.get("qualifica", ""),
                "livello":         dip.get("livello", ""),
                "updated_at":      datetime.utcnow().isoformat(),
            }, "$setOnInsert": {
                "_id":        str(uuid.uuid4()),
                "created_at": datetime.utcnow().isoformat(),
                "attivo":     True,
            }},
            upsert=True,
        )
        contatori["upsert_dipendenti"] += 1

        # Cedolino (dedup per CF + mese)
        mese_ref = dip.get("mese_riferimento", "")
        existing = await db["cedolini"].find_one({
            "codice_fiscale": cf,
            "mese_riferimento": mese_ref,
        })
        if existing:
            contatori["skip"] += 1
            continue

        cedolino = {
            "_id":               str(uuid.uuid4()),
            "doc_inbox_id":      doc_id,
            "codice_fiscale":    cf,
            "nome":              dip.get("nome", ""),
            "cognome":           dip.get("cognome", ""),
            "mese_riferimento":  mese_ref,
            "anno":              dip.get("anno", datetime.now().year),
            "retribuzione_lorda": dip.get("retribuzione_lorda", 0.0),
            "netto_pagare":      dip.get("netto_pagare", 0.0),
            "trattenute":        dip.get("trattenute", []),
            "presenze":          dip.get("presenze", {}),
            "voci_paga":         dip.get("voci_paga", []),
            "iban":              dip.get("iban", ""),
            "created_at":        datetime.utcnow().isoformat(),
        }
        await db["cedolini"].insert_one(cedolino)
        contatori["cedolini_inseriti"] += 1

    # Prima nota salari (una riga per mese, totale netti)
    totale_netti = dati.get("totale_netti", 0.0)
    mese_rif     = dati.get("mese_riferimento", "")
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
                "descrizione":       f"Pagamento salari {mese_rif} — {len(dipendenti_list)} dipendenti",
                "categoria":         "salari",
                "mese_riferimento":  mese_rif,
                "doc_inbox_id":      doc_id,
                "created_at":        datetime.utcnow().isoformat(),
            })

    return {**contatori, "totale_netti": totale_netti, "mese": mese_rif}


# ── Entry point principale ────────────────────────────────────────────────────
async def scan_gmail(giorni_indietro: int = 14, forza_riprocessa: bool = False) -> dict:
    """
    Scansiona Gmail IMAP per allegati PDF da mittenti attendibili.
    Ritorna un report di quanti file trovati/processati/saltati.
    """
    if not GMAIL_USER or not GMAIL_PASSWORD:
        return {"errore": "GMAIL_USER / GMAIL_APP_PASSWORD non configurati"}

    report = {
        "email_esaminate": 0,
        "allegati_trovati": 0,
        "documenti_inseriti": 0,
        "documenti_saltati": 0,
        "errori": [],
    }

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
    except Exception as e:
        return {"errore": f"Login Gmail fallito: {e}"}

    try:
        data_da = (datetime.now() - timedelta(days=giorni_indietro)).strftime("%d-%b-%Y")

        for folder in ["INBOX", "[Gmail]/Tutti i messaggi"]:
            try:
                mail.select(folder)
            except Exception:
                continue

            status, messages = mail.search(None, f'SINCE {data_da}')
            if status != "OK":
                continue

            ids = messages[0].split()
            for uid in ids:
                try:
                    status, msg_data = mail.fetch(uid, "(RFC822)")
                    if status != "OK":
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])
                    from_raw = msg.get("From", "")
                    mittente = _mittente_normalizzato(from_raw)
                    if not mittente:
                        continue

                    # Controlla se mittente attendibile
                    categoria_mit = None
                    for trusted, cat in TRUSTED_SENDERS.items():
                        if trusted in mittente:
                            categoria_mit = cat
                            break
                    if not categoria_mit:
                        continue

                    report["email_esaminate"] += 1
                    subject   = _decode_str(msg.get("Subject", ""))
                    date_str  = msg.get("Date", "")

                    # Estrai allegati PDF
                    for part in msg.walk():
                        if part.get_content_maintype() == "multipart":
                            continue
                        if part.get("Content-Disposition") is None:
                            continue

                        filename = _decode_str(part.get_filename() or "")
                        if not filename.lower().endswith(".pdf"):
                            continue

                        pdf_bytes = part.get_payload(decode=True)
                        if not pdf_bytes:
                            continue

                        report["allegati_trovati"] += 1
                        tipo = _classifica_pdf(filename, subject)

                        try:
                            res = await _salva_documento(
                                filename=filename,
                                pdf_bytes=pdf_bytes,
                                tipo=tipo,
                                mittente=mittente,
                                categoria_mit=categoria_mit,
                                subject=subject,
                                data_email=date_str,
                            )
                            if res.get("skip"):
                                report["documenti_saltati"] += 1
                            else:
                                report["documenti_inseriti"] += 1
                        except Exception as e:
                            report["errori"].append(f"{filename}: {e}")

                except Exception as e:
                    report["errori"].append(f"UID {uid}: {e}")

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return report


async def processa_documento_inbox(doc_id: str) -> dict:
    """Riprocessa manualmente un documento già in inbox."""
    db = get_db()
    from bson import ObjectId

    try:
        oid = ObjectId(doc_id)
    except Exception:
        oid = doc_id

    doc = await db["documents_inbox"].find_one({"_id": oid})
    if not doc:
        return {"errore": "Documento non trovato"}

    pdf_bytes = base64.b64decode(doc.get("pdf_bytes_b64", ""))
    tipo      = doc.get("tipo", "generico")

    return await _auto_processa(str(oid), tipo, pdf_bytes)
