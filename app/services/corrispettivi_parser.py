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
import re
import hashlib
from datetime import datetime
from xml.etree import ElementTree as ET


def _clean_xml_namespaces(xml_content: str) -> str:
    """Rimuove namespace e prefissi tipo n1:Tag, p:Tag dall'XML prima del parsing."""
    if xml_content.startswith('﻿'):
        xml_content = xml_content[1:]
    xml_content = xml_content.replace('\x00', '').strip()
    xml_content = re.sub(r'\s+xmlns(:[a-zA-Z0-9_-]+)?="[^"]*"', '', xml_content)
    xml_content = re.sub(r"\s+xmlns(:[a-zA-Z0-9_-]+)?='[^']*'", '', xml_content)
    xml_content = re.sub(r'\s+xsi:[a-zA-Z]+="[^"]*"', '', xml_content)
    xml_content = re.sub(r"<([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)", r'<\2', xml_content)
    xml_content = re.sub(r"</([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)", r'</\2', xml_content)
    return xml_content


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
    if "T" in raw:
        raw = raw.split("T")[0]
    if len(raw) == 10 and raw[4] == "-":
        return raw
    if "/" in raw and len(raw) == 10:
        parts = raw.split("/")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return raw


def _parse_cor10(root: ET.Element, xml_hash: str) -> dict:
    """Parsa formato COR10 (DatiCorrispettivi — AdE standard v1.0)."""
    # Data: preferisce DataOraRilevazione; per giorni inattivi usa PeriodoInattivo/Dal
    data_raw = ""
    periodo_inattivo = bool(_find_all(root, "PeriodoInattivo"))
    if periodo_inattivo:
        dal_nodes = _find_all(root, "Dal")
        if dal_nodes:
            data_raw = (dal_nodes[0].text or "").strip()
    if not data_raw:
        for tname in ["DataOraRilevazione", "DataOraTrasmissione"]:
            nodes = _find_all(root, tname)
            if nodes and nodes[0].text:
                data_raw = (nodes[0].text or "").strip()
                break

    data = _parse_data(data_raw) if data_raw else datetime.utcnow().strftime("%Y-%m-%d")
    anno = int(data[:4]) if data and len(data) >= 4 else 0

    progressivo = ""
    prog_nodes = _find_all(root, "Progressivo")
    if prog_nodes:
        progressivo = (prog_nodes[0].text or "").strip()

    matricola_rt = ""
    id_nodes = _find_all(root, "IdDispositivo")
    if id_nodes:
        matricola_rt = (id_nodes[0].text or "").strip()

    # Pagamenti da DatiRT/Totali — supporta più nomi usati da diversi produttori RT
    contanti    = 0.0
    elettronico = 0.0
    n_doc       = 0
    _CONT_TAGS = {"PAGATOCONTANTI", "CONTANTE", "CONTANTI", "TOTCONTANTE", "TOTCONTANTI",
                  "TOTALCONTANTI", "PAGAMENTOCONTANTE"}
    _ELETT_TAGS = {"PAGATOELETTRONICO", "ELETTRONICO", "PAGAMENTOELETTRONICO",
                   "TOTELETTRONICO", "TOTALELETTRONICO", "PAGAMENTIELETTRONICI",
                   "PAGAMENTIMEDIOELETTRONICO", "POSELETTRONICO"}
    totali_nodes = _find_all(root, "Totali")
    if totali_nodes:
        t = totali_nodes[0]
        for child in t:
            local = (child.tag.split("}")[-1] if "}" in child.tag else child.tag).upper()
            if local in _CONT_TAGS:
                contanti = _safe_float(child.text or "0")
            elif local in _ELETT_TAGS:
                elettronico = _safe_float(child.text or "0")
            elif local == "NUMERODOCCOMMERCIALI":
                n_doc = int(_safe_float(child.text or "0"))

    # Fallback: se Totali non trovati, cerca DatiPagamento come nel formato RT classico
    if contanti == 0.0 and elettronico == 0.0:
        for pag in _find_all(root, "DatiPagamento"):
            modalita = ""
            importo  = 0.0
            for child in pag:
                local = (child.tag.split("}")[-1] if "}" in child.tag else child.tag).upper()
                if local in ("MODALITAPAGAMENTO", "MODALITA", "TIPOPAGAMENTO"):
                    modalita = (child.text or "").strip().upper()
                if local in ("IMPORTO", "IMPORTOPAGAMENTO"):
                    importo = _safe_float(child.text or "0")
            if any(k in modalita for k in ("CONTANT", "CASH", "MP01")):
                contanti += importo
            elif any(k in modalita for k in ("ELETTRON", "POS", "CARTA", "BANCOMAT",
                                              "CREDIT", "SATISPAY", "PAYPAL", "MP02", "MP08")):
                elettronico += importo

    totale = round(contanti + elettronico, 2)

    # IVA da DatiRT/Riepilogo
    totale_imponibile = 0.0
    totale_iva        = 0.0
    aliquote_iva      = []
    for riep in _find_all(root, "Riepilogo"):
        ammontare = _safe_float(_tag(riep, "Ammontare"))
        iva_els = [c for c in riep if (c.tag.split("}")[-1] if "}" in c.tag else c.tag).upper() == "IVA"]
        if iva_els:
            aliquota = _safe_float(_tag(iva_els[0], "AliquotaIVA"))
            imposta  = _safe_float(_tag(iva_els[0], "Imposta"))
        else:
            aliquota = 0.0
            imposta  = 0.0
        totale_imponibile += ammontare
        totale_iva        += imposta
        if aliquota > 0 and ammontare > 0:
            aliquote_iva.append({
                "aliquota":   aliquota,
                "imponibile": round(ammontare, 2),
                "imposta":    round(imposta, 2),
            })

    return {
        "xml_hash":           xml_hash,
        "data":               data,
        "anno":               anno,
        "numero_documento":   progressivo,
        "matricola_rt":       matricola_rt,
        "numero_doc_comm":    n_doc,
        "periodo_inattivo":   periodo_inattivo,
        "totale":             totale,
        "totale_imponibile":  round(totale_imponibile, 2),
        "totale_iva":         round(totale_iva, 2),
        "contanti":           round(contanti, 2),
        "elettronico":        round(elettronico, 2),
        "altri":              0.0,
        "aliquote_iva":       aliquote_iva,
        "pos_dichiarato":     None,
        "pos_banca":          None,
        "discrepanza":        None,
        "prima_nota_cassa_id":      None,
        "prima_nota_uscita_pos_id": None,
        "source":             "xml_cor10",
        "created_at":         datetime.utcnow().isoformat(),
    }


def parse_corrispettivo_xml(xml_bytes: bytes) -> dict:
    """
    Parsa un XML di corrispettivi giornalieri dal registratore telematico.
    Supporta sia il vecchio formato RT sia il formato COR10 (AdE v1.0).
    """
    xml_hash = hashlib.md5(xml_bytes).hexdigest()

    root = None
    for xml_src in (xml_bytes, _clean_xml_namespaces(xml_bytes.decode("utf-8", errors="replace")).encode("utf-8")):
        try:
            root = ET.fromstring(xml_src)
            break
        except ET.ParseError:
            continue
    if root is None:
        raise ValueError("XML corrispettivi non valido o namespace non riconosciuto")

    # Rilevamento formato COR10 (DatiCorrispettivi)
    root_local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    versione   = root.get("versione", "")
    if root_local.upper() == "DATICORRISPETTIVI" or "COR" in versione.upper():
        return _parse_cor10(root, xml_hash)

    # ── Vecchio formato RT — Data documento ───────────────────────────────────
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
