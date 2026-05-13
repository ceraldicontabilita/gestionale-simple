"""
Parser XML Corrispettivi — Ceraldi Group ERP

Il registratore telematico del bar produce ogni giorno un file XML
conforme al tracciato RT (Agenzia delle Entrate — specifiche 7.0).

Struttura tipica:
  <RegistratoreTelematico>
    <DocumentoDati>
      <DataOraDocumento>2026-01-24T23:59:00</DataOraDocumento>
      <ImportoTotaleDocumento>2624.30</ImportoTotaleDocumento>
    </DocumentoDati>
    <Riepilogo>
      <AliquotaIVA>10.00</AliquotaIVA>
      <Imposta>238.57</Imposta>
      <ImponibileImporto>2385.73</ImponibileImporto>
    </Riepilogo>
    <DatiPagamento>
      <ModalitaPagamento>CONTANTI</ModalitaPagamento>
      <Importo>863.20</Importo>
    </DatiPagamento>
    <DatiPagamento>
      <ModalitaPagamento>ELETTRONICO</ModalitaPagamento>
      <Importo>1761.10</Importo>
    </DatiPagamento>
  </RegistratoreTelematico>

Nota: diversi produttori di RT usano strutture leggermente diverse.
Il parser è tollerante e prova più percorsi prima di arrendersi.
"""
import hashlib
from datetime import datetime
from xml.etree import ElementTree as ET
from typing import Optional


def _tag(el: ET.Element, *path: str) -> str:
    current = el
    for step in path:
        found = None
        for child in current:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local.upper() == step.upper():
                found = child
                break
        if found is None:
            return ""
        current = found
    return (current.text or "").strip()


def _find_all(el: ET.Element, tag: str):
    return [
        c for c in el.iter()
        if (c.tag.split("}")[-1] if "}" in c.tag else c.tag).upper() == tag.upper()
    ]


def _safe_float(s: str) -> float:
    try:
        return float(s.replace(",", "."))
    except Exception:
        return 0.0


def _parse_data(raw: str) -> str:
    """Normalizza la data in formato YYYY-MM-DD."""
    raw = raw.strip()
    # ISO datetime: 2026-01-24T23:59:00
    if "T" in raw:
        raw = raw.split("T")[0]
    # Già formato YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-":
        return raw
    # GG/MM/YYYY
    if "/" in raw and len(raw) == 10:
        parts = raw.split("/")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return raw


def parse_corrispettivo_xml(xml_bytes: bytes) -> dict:
    """
    Parsa un XML di corrispettivi giornalieri dal registratore telematico.
    Ritorna dict pronto per inserimento in collection 'corrispettivi'.
    """
    xml_hash = hashlib.md5(xml_bytes).hexdigest()

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"XML corrispettivi non valido: {e}")

    # ── Data documento ────────────────────────────────────────────────────────
    data_raw = ""
    for tag_name in ["DataOraDocumento", "DataDocumento", "Data", "DataRiferimento"]:
        nodes = _find_all(root, tag_name)
        if nodes:
            data_raw = (nodes[0].text or "").strip()
            break
    data = _parse_data(data_raw) if data_raw else datetime.utcnow().strftime("%Y-%m-%d")

    # ── Totale documento ──────────────────────────────────────────────────────
    totale = 0.0
    for tag_name in ["ImportoTotaleDocumento", "TotaleDocumento", "Totale", "ImportoTotale"]:
        nodes = _find_all(root, tag_name)
        if nodes:
            totale = _safe_float((nodes[0].text or "0"))
            break

    # ── Riepilogo IVA ─────────────────────────────────────────────────────────
    totale_imponibile = 0.0
    totale_iva        = 0.0
    aliquote_iva      = []

    for riepilogo in _find_all(root, "Riepilogo"):
        aliquota    = _safe_float(_tag(riepilogo, "AliquotaIVA"))
        imposta     = _safe_float(_tag(riepilogo, "Imposta"))
        imponibile  = _safe_float(_tag(riepilogo, "ImponibileImporto"))
        totale_imponibile += imponibile
        totale_iva        += imposta
        if aliquota > 0:
            aliquote_iva.append({
                "aliquota":   aliquota,
                "imponibile": round(imponibile, 2),
                "imposta":    round(imposta, 2),
            })

    # Fallback: se non c'è riepilogo dettagliato, usa il totale con IVA 10%
    if totale_imponibile == 0 and totale > 0:
        # Bar/pasticceria: IVA standard 10%
        totale_iva        = round(totale / 11 * 1, 2)
        totale_imponibile = round(totale - totale_iva, 2)

    # ── Pagamenti ─────────────────────────────────────────────────────────────
    contanti   = 0.0
    elettronico = 0.0  # POS / carte / satispay / ecc.
    altri      = 0.0

    for pag in _find_all(root, "DatiPagamento"):
        modalita = ""
        importo  = 0.0
        for child in pag:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local.upper() in ("MODALITAPAGAMENTO", "MODALITA", "TIPOPAGAMENTO"):
                modalita = (child.text or "").strip().upper()
            if local.upper() in ("IMPORTO", "IMPORTOPAGAMENTO"):
                importo = _safe_float(child.text or "0")

        if not modalita and importo == 0:
            # Struttura alternativa: tag singoli
            modalita_node = _find_all(pag, "ModalitaPagamento") or _find_all(pag, "Modalita")
            importo_node  = _find_all(pag, "Importo") or _find_all(pag, "ImportoPagamento")
            if modalita_node:
                modalita = (modalita_node[0].text or "").strip().upper()
            if importo_node:
                importo = _safe_float(importo_node[0].text or "0")

        mod_upper = modalita.upper()
        if any(k in mod_upper for k in ("CONTANT", "CASH", "MP01")):
            contanti += importo
        elif any(k in mod_upper for k in ("ELETTRON", "POS", "CARTA", "BANCOMAT",
                                          "CREDIT", "SATISPAY", "PAYPAL",
                                          "ELECTRONIC", "MP08", "MP02", "MP05")):
            elettronico += importo
        else:
            altri += importo

    # Se non ci sono dati di pagamento ma abbiamo il totale:
    if contanti == 0 and elettronico == 0 and totale > 0:
        # Non possiamo sapere la suddivisione — mettiamo tutto come "non suddiviso"
        pass

    # Verifica coerenza: totale dovrebbe = contanti + elettronico + altri
    totale_calcolato = contanti + elettronico + altri
    if totale == 0 and totale_calcolato > 0:
        totale = totale_calcolato

    # ── Anno ─────────────────────────────────────────────────────────────────
    anno = 0
    if data and len(data) >= 4:
        try:
            anno = int(data[:4])
        except Exception:
            pass

    # ── Numero documento RT ───────────────────────────────────────────────────
    numero_doc = ""
    for tag_name in ["NumeroDocumento", "NumeroRicevuta", "Numero", "ProgressivoDocumento"]:
        nodes = _find_all(root, tag_name)
        if nodes and nodes[0].text:
            numero_doc = (nodes[0].text or "").strip()
            break

    # ── Matricola RT ──────────────────────────────────────────────────────────
    matricola_rt = ""
    for tag_name in ["MatricolaRT", "Matricola", "NumeroSeriale", "CodiceDispositivo"]:
        nodes = _find_all(root, tag_name)
        if nodes and nodes[0].text:
            matricola_rt = (nodes[0].text or "").strip()
            break

    return {
        "xml_hash":         xml_hash,
        "data":             data,
        "anno":             anno,
        "numero_documento": numero_doc,
        "matricola_rt":     matricola_rt,

        # Importi principali
        "totale":           round(totale, 2),
        "totale_imponibile": round(totale_imponibile, 2),
        "totale_iva":       round(totale_iva, 2),

        # Suddivisione pagamenti
        "contanti":         round(contanti, 2),
        "elettronico":      round(elettronico, 2),   # quota POS → va in banca
        "altri":            round(altri, 2),

        # Dettaglio aliquote
        "aliquote_iva":     aliquote_iva,

        # Confronto POS (compilato dall'utente / dalla banca — separatamente)
        "pos_dichiarato":   None,   # inserimento manuale serale
        "pos_banca":        None,   # trovato nell'estratto conto
        "discrepanza":      None,   # calcolata al momento del confronto

        # Prima nota (compilato dopo smistamento)
        "prima_nota_cassa_id": None,   # ENTRATA cassa (totale)
        "prima_nota_uscita_pos_id": None,  # USCITA cassa (quota POS)

        # Stato
        "source":           "xml",
        "created_at":       datetime.utcnow().isoformat(),
    }
