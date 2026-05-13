"""
Parser FatturaPA — Ceraldi Group ERP
Gestisce XML puro e file P7M (busta crittografica CAdES).
Restituisce sempre un dict uniforme pronto per inserimento in 'invoices'.
"""
import re
import hashlib
import subprocess
import tempfile
import os
from datetime import datetime
from xml.etree import ElementTree as ET
from typing import Optional


# ── Costanti IVA detraibilità (Art. 19-bis1 DPR 633/72) ─────────────────────
DETRAIBILITA_MAP = {
    "alimentari":   100.0,
    "noleggio":      40.0,
    "energia":      100.0,
    "telefonia":     50.0,
    "riparazioni":   40.0,
    "carburante":    40.0,
    "assicurazione":  0.0,
    "generico":     100.0,
}

# Parole chiave nelle descrizioni linee per classificare noleggio
KEYWORDS_NOLEGGIO = {
    "canone noleggio":      "noleggio",
    "rata noleggio":        "noleggio",
    "noleggio auto":        "noleggio",
    "noleggio veicolo":     "noleggio",
    "tassa possesso":       "tassa_possesso",
    "bollo":                "tassa_possesso",
    "riparazione":          "riparazione",
    "carrozzeria":          "riparazione",
    "manutenzione":         "riparazione",
    "penale":               "penale",
    "sinistro":             "sinistro",
    "franchigia":           "sinistro",
    "gomme":                "riparazione",
    "pneumatici":           "riparazione",
}

REGEX_TARGA = re.compile(r'\b([A-Z]{2}\d{3}[A-Z]{2})\b')


# ── Decodifica P7M ────────────────────────────────────────────────────────────

def decode_p7m(data: bytes) -> bytes:
    """Tenta 3 metodi in cascata per estrarre XML da file P7M."""

    # Metodo 1: OpenSSL
    try:
        with tempfile.NamedTemporaryFile(suffix=".p7m", delete=False) as f:
            f.write(data)
            tmp_in = f.name
        tmp_out = tmp_in + ".xml"
        result = subprocess.run(
            ["openssl", "smime", "-verify", "-noverify", "-in", tmp_in,
             "-inform", "DER", "-out", tmp_out],
            capture_output=True, timeout=10
        )
        if result.returncode == 0 and os.path.exists(tmp_out):
            with open(tmp_out, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in [tmp_in, tmp_out]:
            try:
                os.unlink(p)
            except Exception:
                pass

    # Metodo 2: asn1crypto
    try:
        from asn1crypto import cms
        ci = cms.ContentInfo.load(data)
        if ci["content_type"].native == "signed_data":
            sd = ci["content"].parsed
            return sd["encap_content_info"]["content"].parsed.contents
    except Exception:
        pass

    # Metodo 3: ricerca byte (XML inizia con <?xml o <Fattura)
    for marker in [b"<?xml", b"<FatturaElettronica", b"<p:FatturaElettronica"]:
        idx = data.find(marker)
        if idx != -1:
            return data[idx:]

    raise ValueError("Impossibile decodificare P7M con nessun metodo")


# ── Parsing XML FatturaPA ─────────────────────────────────────────────────────

def _tag(el: ET.Element, *path: str) -> str:
    """Estrae testo da un percorso di tag, ignorando namespace."""
    current = el
    for step in path:
        found = None
        for child in current:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local == step:
                found = child
                break
        if found is None:
            return ""
        current = found
    return (current.text or "").strip()


def _find_all(el: ET.Element, tag: str):
    """Trova tutti i figli con quel tag locale (ignora namespace)."""
    return [
        c for c in el.iter()
        if (c.tag.split("}")[-1] if "}" in c.tag else c.tag) == tag
    ]


def _safe_float(s: str) -> float:
    try:
        return float(s.replace(",", "."))
    except Exception:
        return 0.0


def parse_fattura_xml(xml_bytes: bytes) -> dict:
    """
    Parsa un XML FatturaPA e restituisce un dict con tutti i campi.
    Compatible con i nomi campo esistenti nella collection 'invoices'.
    """
    xml_hash = hashlib.md5(xml_bytes).hexdigest()

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"XML non valido: {e}")

    # ── Header ────────────────────────────────────────────────────────────────
    trasmissione = _find_all(root, "DatiTrasmissione")
    id_trasmissione = ""
    if trasmissione:
        id_trasmissione = _tag(trasmissione[0], "ProgressivoInvio")

    tipo_doc = ""
    dati_gen = _find_all(root, "DatiGeneraliDocumento")
    if dati_gen:
        tipo_doc = _tag(dati_gen[0], "TipoDocumento")

    # ── Cedente (fornitore) ───────────────────────────────────────────────────
    cedente_nodes = _find_all(root, "CedentePrestatore")
    fornitore_piva = ""
    fornitore_cf   = ""
    fornitore_nome = ""
    fornitore_ind  = ""
    fornitore_cap  = ""
    fornitore_citta = ""
    fornitore_iban = ""

    if cedente_nodes:
        cn = cedente_nodes[0]
        id_fiscale = _find_all(cn, "IdFiscaleIVA")
        if id_fiscale:
            fornitore_piva = _tag(id_fiscale[0], "IdCodice")
        fornitore_cf    = _tag(cn, "CodiceFiscale")
        dati_anag       = _find_all(cn, "DatiAnagrafici")
        if dati_anag:
            fornitore_nome = (
                _tag(dati_anag[0], "Denominazione") or
                f"{_tag(dati_anag[0], 'Nome')} {_tag(dati_anag[0], 'Cognome')}".strip()
            )
        sede_nodes = _find_all(cn, "Sede")
        if sede_nodes:
            fornitore_ind   = _tag(sede_nodes[0], "Indirizzo")
            fornitore_cap   = _tag(sede_nodes[0], "CAP")
            fornitore_citta = _tag(sede_nodes[0], "Comune")

    # IBAN dalla sezione pagamento
    dati_pag = _find_all(root, "DatiPagamento")
    metodo_pag_xml = ""
    data_scadenza  = ""
    if dati_pag:
        metodo_pag_xml = _tag(dati_pag[0], "ModalitaPagamento")
        det_pag = _find_all(dati_pag[0], "DettaglioPagamento")
        if det_pag:
            fornitore_iban = _tag(det_pag[0], "IBAN")
            data_scadenza  = _tag(det_pag[0], "DataScadenzaPagamento")

    # ── Dati documento ────────────────────────────────────────────────────────
    numero_fattura = ""
    data_fattura   = ""
    causale        = ""
    if dati_gen:
        numero_fattura = _tag(dati_gen[0], "Numero")
        data_fattura   = _tag(dati_gen[0], "Data")
        causali        = _find_all(dati_gen[0], "Causale")
        causale        = " | ".join(c.text or "" for c in causali)

    # ── Righe dettaglio ───────────────────────────────────────────────────────
    linee = []
    totale_imponibile = 0.0
    totale_iva        = 0.0
    aliquota_iva_principale = 0.0
    targhe_estratte   = set()
    categorie_linee   = set()

    for linea in _find_all(root, "DettaglioLinee"):
        desc      = _tag(linea, "Descrizione")
        qty       = _safe_float(_tag(linea, "Quantita") or "1")
        prezzo    = _safe_float(_tag(linea, "PrezzoUnitario"))
        imponibile = _safe_float(_tag(linea, "PrezzoTotale"))
        aliquota  = _safe_float(_tag(linea, "AliquotaIVA"))
        iva_linea = imponibile * aliquota / 100

        totale_imponibile += imponibile
        totale_iva        += iva_linea
        if aliquota > aliquota_iva_principale:
            aliquota_iva_principale = aliquota

        # Cerca targa nella descrizione
        m = REGEX_TARGA.search(desc.upper())
        if m:
            targhe_estratte.add(m.group(1))

        # Classifica linea per tipo noleggio
        desc_lower = desc.lower()
        cat_linea = "generico"
        for kw, cat in KEYWORDS_NOLEGGIO.items():
            if kw in desc_lower:
                cat_linea = cat
                break
        categorie_linee.add(cat_linea)

        linee.append({
            "descrizione": desc,
            "quantita":    qty,
            "prezzo_unitario": prezzo,
            "imponibile":  imponibile,
            "aliquota_iva": aliquota,
            "iva":         round(iva_linea, 2),
            "categoria":   cat_linea,
        })

    # Riepilogo IVA dal documento (più affidabile delle linee)
    for riepilogo in _find_all(root, "DatiRiepilogo"):
        imponibile_riepilogo = _safe_float(_tag(riepilogo, "ImponibileImporto"))
        iva_riepilogo        = _safe_float(_tag(riepilogo, "Imposta"))
        aliquota_riepilogo   = _safe_float(_tag(riepilogo, "AliquotaIVA"))
        if imponibile_riepilogo > 0:
            totale_imponibile = imponibile_riepilogo
        if iva_riepilogo > 0:
            totale_iva = iva_riepilogo
        if aliquota_riepilogo > 0:
            aliquota_iva_principale = aliquota_riepilogo

    totale_fattura = totale_imponibile + totale_iva

    # ── Categoria IVA dominante per detraibilità ──────────────────────────────
    # Priorità: noleggio > riparazione > tassa_possesso > generico
    cat_priorita = ["noleggio", "riparazione", "tassa_possesso", "penale",
                    "sinistro", "carburante", "alimentari", "energia",
                    "telefonia", "assicurazione", "generico"]
    categoria_iva = "generico"
    for c in cat_priorita:
        if c in categorie_linee:
            categoria_iva = c
            break

    detraibilita = DETRAIBILITA_MAP.get(categoria_iva, 100.0)

    # ── Note credito: DatiFattureCollegate ────────────────────────────────────
    fatture_collegate = []
    for fc in _find_all(root, "DatiFattureCollegate"):
        fatture_collegate.append({
            "numero": _tag(fc, "NumeroFattura"),
            "data":   _tag(fc, "DataFattura"),
        })

    # ── Anno ─────────────────────────────────────────────────────────────────
    anno = 0
    if data_fattura and len(data_fattura) >= 4:
        try:
            anno = int(data_fattura[:4])
        except Exception:
            pass

    return {
        # Identificatori
        "xml_hash":          xml_hash,
        "id_trasmissione":   id_trasmissione,
        "numero_fattura":    numero_fattura,
        "data_fattura":      data_fattura,
        "anno":              anno,
        "tipo_documento":    tipo_doc,          # TD01, TD04, TD24, ecc.
        "causale":           causale,

        # Fornitore
        "supplier_vat":      fornitore_piva,    # nome campo esistente nel DB
        "fornitore_piva":    fornitore_piva,
        "fornitore_cf":      fornitore_cf,
        "fornitore_nome":    fornitore_nome,
        "fornitore_indirizzo": fornitore_ind,
        "fornitore_cap":     fornitore_cap,
        "fornitore_citta":   fornitore_citta,
        "fornitore_iban":    fornitore_iban,

        # Importi
        "importo_imponibile": round(totale_imponibile, 2),
        "importo_iva":        round(totale_iva, 2),
        "importo_totale":     round(totale_fattura, 2),
        "aliquota_iva":       aliquota_iva_principale,

        # IVA detraibilità
        "categoria_iva":      categoria_iva,
        "detraibilita_pct":   detraibilita,
        "iva_detraibile":     round(totale_iva * detraibilita / 100, 2),

        # Pagamento
        "metodo_pagamento_xml": metodo_pag_xml,
        "data_scadenza":      data_scadenza,

        # Linee dettaglio
        "linee_dettaglio":    linee,

        # Extra
        "targhe_veicoli":     list(targhe_estratte),
        "fatture_collegate":  fatture_collegate,

        # Stato (valorizzato dopo)
        "stato":              "da_pagare",
        "prima_nota_id":      None,
        "prima_nota_tipo":    None,     # "cassa" | "banca" | "provvisorio"
        "source":             "pec",    # sovrascritto se upload manuale
        "created_at":         datetime.utcnow().isoformat(),
    }


def parse_file(filename: str, data: bytes) -> dict:
    """Entry point: accetta sia XML puro che P7M."""
    if filename.lower().endswith(".p7m"):
        xml_bytes = decode_p7m(data)
    else:
        xml_bytes = data
    result = parse_fattura_xml(xml_bytes)
    result["raw_xml"] = xml_bytes.decode("utf-8", errors="replace")
    return result
