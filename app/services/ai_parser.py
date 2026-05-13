"""
AI Parser — Ceraldi Group ERP
Usa Claude (Anthropic) per estrarre dati strutturati da:
  - PDF F24 (codici tributo, importi, periodi)
  - PDF Verbali/Multe (targa, importo, numero verbale)
  - PDF Cedolini (dipendente, netto, TFR)
  - Testo generico (classificazione documento)

Modello: claude-3-5-haiku (veloce + economico per estrazione dati)
"""
import os
import base64
import json
import re
from datetime import datetime
from typing import Optional
import anthropic
from dotenv import load_dotenv

load_dotenv()

_client: Optional[anthropic.Anthropic] = None
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", "")
        )
    return _client


def _pdf_to_b64(pdf_bytes: bytes) -> str:
    return base64.standard_b64encode(pdf_bytes).decode("utf-8")


# ── F24 Parser ───────────────────────────────────────────────────────────────

async def parse_f24(pdf_bytes: bytes) -> dict:
    """
    Estrae da un PDF F24 (modello Agenzia delle Entrate):
    Ritorna dict con:
      - codici_tributo: lista piatta di tutti i codici da tutte le sezioni
      - totale_saldo: float
      - scadenza: YYYY-MM-DD (data versamento)
      - periodo: es. "04/2026"
      - sezioni_raw: struttura originale per dettagli
    """
    prompt = """Sei un commercialista italiano esperto. Analizza questo modello F24 ed estrai TUTTI i dati.
Il F24 ha sezioni: Erario (codici 1001, 1701, 1704, 1075, ecc.), INPS (5100 ecc.), Regioni (3802 ecc.),
IMU/TARI, INAIL.

Rispondi SOLO con JSON valido, nessun testo fuori dal JSON:
{
  "scadenza": "YYYY-MM-DD",
  "periodo": "MM/YYYY o stringa periodo trovata",
  "codice_fiscale_contribuente": "",
  "totale_saldo": 0.00,
  "codici_tributo": [
    {
      "sezione": "Erario|INPS|Regioni|IMU|INAIL",
      "codice_tributo": "es. 1001",
      "descrizione": "es. IRPEF ritenute lavoro dipendente",
      "mese_riferimento": "01-12 o vuoto",
      "anno_riferimento": "YYYY o vuoto",
      "importo_debito": 0.00,
      "importo_credito": 0.00,
      "codice_ente": "es. F839 per Napoli, matric INPS, cod.ditta INAIL"
    }
  ],
  "note": "anomalie o dati mancanti"
}

IMPORTANTE: includi TUTTI i codici trovati nel documento, anche quelli con credito (importo_credito > 0).
Il totale_saldo è il saldo finale dopo compensazioni (debiti - crediti)."""

    try:
        response = get_client().messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _pdf_to_b64(pdf_bytes),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        # Rimuovi eventuale markdown ```json ... ```
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"errore": "JSON non valido nella risposta AI", "raw": raw[:500]}
    except Exception as e:
        return {"errore": str(e)}


# ── Verbale/Multa Parser ─────────────────────────────────────────────────────

async def parse_verbale(pdf_bytes: bytes) -> dict:
    """
    Estrae da un PDF verbale/multa:
    - numero_verbale: es. "B25120309770"
    - targa: es. "GG782PN"
    - data_verbale: "YYYY-MM-DD"
    - importo: float
    - ente_emittente: es. "Comune di Napoli"
    - scadenza_pagamento: "YYYY-MM-DD"
    - iuv: codice IUV per PagoPA
    """
    prompt = """Sei un esperto di verbali stradali italiani. Analizza questo documento ed estrai i dati.

Rispondi SOLO con JSON valido:
{
  "numero_verbale": "",
  "targa": "",
  "data_verbale": "YYYY-MM-DD",
  "importo_originale": 0.00,
  "importo_ridotto": 0.00,
  "scadenza_pagamento": "YYYY-MM-DD",
  "ente_emittente": "",
  "articolo_violato": "",
  "luogo_infrazione": "",
  "iuv": "",
  "cbill": "",
  "note": ""
}

IMPORTANTE: la targa è in formato italiano es. AA123BB o AA000AA."""

    try:
        response = get_client().messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _pdf_to_b64(pdf_bytes),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        # Normalizza targa uppercase
        if result.get("targa"):
            result["targa"] = result["targa"].upper().replace(" ", "")
        return result
    except json.JSONDecodeError:
        return {"errore": "JSON non valido", "raw": raw}
    except Exception as e:
        return {"errore": str(e)}


# ── Cedolino Parser ──────────────────────────────────────────────────────────

async def parse_cedolino(pdf_bytes: bytes) -> dict:
    """
    Estrae da un PDF Libro Unico / cedolino (formato Zucchetti).
    Ritorna dict con:
      - dipendenti: lista di cedolini individuali
      - totale_lordo, totale_netti: float
      - mese_riferimento: "YYYY-MM"
      - anno: int
    """
    prompt = """Sei un esperto di buste paga italiane (software Zucchetti/TeamSystem).
Analizza questo Libro Unico del Lavoro o riepilogo paghe ed estrai i dati di TUTTI i dipendenti presenti.
Nel formato Zucchetti ogni dipendente ha: matricola, nome/cognome, codice fiscale, IBAN,
voci paga (retribuzione base, scatti, indennità), trattenute (IRPEF, contributi INPS dipendente),
presenze mensili (calendario con codici: ORD=ordinario, FE=ferie, MA=malattia, AI=assenza ingiustificata, PE=permesso, STR=straordinario).

Rispondi SOLO con JSON valido — un oggetto contenente la lista dipendenti:
{
  "mese_riferimento": "YYYY-MM",
  "anno": 2026,
  "totale_lordo": 0.00,
  "totale_netti": 0.00,
  "totale_contributi_azienda": 0.00,
  "n_dipendenti": 0,
  "dipendenti": [
    {
      "codice_fiscale": "",
      "matricola": "",
      "cognome": "",
      "nome": "",
      "qualifica": "",
      "livello": "",
      "iban": "",
      "mese_riferimento": "YYYY-MM",
      "anno": 2026,
      "retribuzione_lorda": 0.00,
      "netto_pagare": 0.00,
      "irpef": 0.00,
      "contributi_dipendente": 0.00,
      "contributi_azienda": 0.00,
      "tfr_mese": 0.00,
      "voci_paga": [{"voce": "", "importo": 0.00}],
      "presenze": {
        "giorni_lavorati": 0,
        "ore_ordinarie": 0,
        "ore_straordinario": 0,
        "giorni_ferie": 0,
        "giorni_malattia": 0,
        "giorni_assenza": 0,
        "giorni_permesso": 0
      },
      "trattenute": [{"voce": "", "importo": 0.00}]
    }
  ]
}

IMPORTANTE: estrai TUTTI i dipendenti presenti nel documento, anche se è un Libro Unico con molte pagine."""

    try:
        response = get_client().messages.create(
            model=MODEL,
            max_tokens=6000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": _pdf_to_b64(pdf_bytes),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        # Normalizza: se Claude ha restituito una lista invece di un dict
        if isinstance(data, list):
            dipendenti = data
            totale_netti  = sum(d.get("netto_pagare", d.get("netto", 0.0)) for d in dipendenti)
            totale_lordo  = sum(d.get("retribuzione_lorda", d.get("lordo", 0.0)) for d in dipendenti)
            mese_rif = ""
            if dipendenti:
                m = dipendenti[0].get("mese_riferimento", "")
                if not m:
                    anno = dipendenti[0].get("anno", datetime.now().year)
                    mese = dipendenti[0].get("mese", datetime.now().month)
                    m    = f"{anno}-{str(mese).zfill(2)}"
                mese_rif = m
            data = {
                "mese_riferimento": mese_rif,
                "anno": dipendenti[0].get("anno", datetime.now().year) if dipendenti else datetime.now().year,
                "totale_lordo":  totale_lordo,
                "totale_netti":  totale_netti,
                "n_dipendenti":  len(dipendenti),
                "dipendenti":    dipendenti,
            }
        return data
    except json.JSONDecodeError:
        return {"errore": "JSON non valido", "dipendenti": [], "totale_netti": 0.0}
    except Exception as e:
        return {"errore": str(e), "dipendenti": [], "totale_netti": 0.0}




# ── Classificatore email/documento generico ───────────────────────────────────

async def classifica_documento(testo: str, mittente: str = "") -> dict:
    """
    Dato il testo di un'email o documento, classifica il tipo e estrae
    informazioni chiave per lo smistamento automatico.
    """
    prompt = f"""Sei un assistente contabile italiano. Classifica questo documento aziendale.

Mittente: {mittente}
Testo: {testo[:3000]}

Rispondi SOLO con JSON:
{{
  "tipo": "f24|cedolino|verbale|pagopa|cartella_esattoriale|tari|inps|inail|fattura|assicurazione|altro",
  "urgente": true/false,
  "importo": 0.00,
  "scadenza": "YYYY-MM-DD o null",
  "riferimento": "numero documento o riferimento chiave",
  "azione_richiesta": "descrizione breve dell'azione necessaria",
  "note": ""
}}"""

    try:
        response = get_client().messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        return {"tipo": "altro", "errore": str(e)}


# ── Lookup ATECO → Categoria IVA ─────────────────────────────────────────────

async def classifica_fornitore_da_piva(piva: str, nome: str) -> dict:
    """
    Dato P.IVA e nome fornitore, determina:
    - categoria_iva: alimentari|noleggio|energia|telefonia|riparazioni|generico
    - detraibilita_pct: 0-100
    - ateco_suggerito: codice ATECO probabile
    Usa solo la logica del nome quando non ha accesso a ATECO reale.
    """
    prompt = f"""Sei un commercialista italiano esperto di IVA (Art. 19-bis1 DPR 633/72).

Azienda fornitore:
- P.IVA: {piva}
- Nome: {nome}

In base al nome del fornitore, classifica la sua categoria e indica la detraibilità IVA corretta.

Rispondi SOLO con JSON:
{{
  "categoria_iva": "alimentari|noleggio|energia|telefonia|riparazioni|assicurazione|carburante|generico",
  "detraibilita_pct": 100,
  "motivazione": "breve spiegazione",
  "ateco_suggerito": "es. 46.39 o 77.11"
}}

Regole:
- Leasys, ALD, Arval, LeasePlan → noleggio → 40%
- Enel, Eni gas, Edison, A2A → energia → 100%
- TIM, Vodafone, Fastweb, Wind → telefonia → 50%
- Esso, IP, Q8, TotalErg, ENI → carburante → 40%
- Autoriparazioni, carrozzerie → riparazioni → 40%
- Generali, UnipolSai, Allianz → assicurazione → 0%
- Bar, ristoranti, alimentari, grossisti alimentari → alimentari → 100%
- Tutto il resto generico → 100%"""

    try:
        response = get_client().messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        return {
            "categoria_iva": "generico",
            "detraibilita_pct": 100.0,
            "motivazione": f"Errore AI: {e}",
            "ateco_suggerito": "",
        }
