"""
Smistamento automatico Prima Nota — Ceraldi Group ERP

Regole:
  MP01 (contanti)             → prima_nota_cassa
  MP02/MP05 (bonifico/SEPA)   → prima_nota_banca
  MP08 (assegno)              → assegni  (+ prima_nota_banca con flag assegno)
  MP12 (riba) / vuoto / altro → prima_nota_provvisori

Il metodo viene SEMPRE dal fornitore in anagrafica, mai dall'XML.
Se il fornitore non ha metodo → provvisori.
Idempotente: stesso fattura_id non crea duplicati.
"""
import uuid
from datetime import datetime
from bson import ObjectId
from typing import Optional

from app.database import (
    col_invoices, col_fornitori,
    col_pn_cassa, col_pn_banca, col_pn_provvisori, col_assegni
)

# Codici MP che vanno in cassa
MP_CASSA  = {"MP01"}
# Codici MP che vanno in banca
MP_BANCA  = {"MP02", "MP05", "MP03", "MP04", "MP06", "MP07", "MP09",
              "MP10", "MP11", "MP14", "MP15", "MP16", "MP17", "MP18",
              "MP19", "MP20", "MP21", "MP22", "MP23"}
# Codici MP assegno (banca con flag assegno)
MP_ASSEGNO = {"MP08"}
# Tutto il resto → provvisori
# (MP12=riba, vuoto, sconosciuto)


def _nuovo_id() -> str:
    return str(uuid.uuid4())


def _segno(tipo_documento: str) -> float:
    """Le note credito TD04/TD08 hanno importo negativo."""
    if tipo_documento in ("TD04", "TD08"):
        return -1.0
    return 1.0


async def smista_fattura(
    fattura_id: str,
    fattura: dict,
    metodo_pagamento: str = "",
) -> dict:
    """
    Dato un fattura_id e il dict fattura, crea il movimento nella collection giusta.
    Ritorna: {destinazione, movimento_id, motivo}
    """
    # Idempotenza: se esiste già un movimento per questa fattura non duplicare
    existing = await _trova_movimento_esistente(fattura_id)
    if existing:
        return {
            "destinazione": existing["destinazione"],
            "movimento_id": existing["movimento_id"],
            "motivo":       "già_registrato",
        }

    mp = (metodo_pagamento or "").strip().upper()
    tipo_doc = fattura.get("tipo_documento", "TD01")
    segno = _segno(tipo_doc)
    importo = round(fattura.get("importo_totale", 0.0) * segno, 2)

    if mp in MP_CASSA:
        return await _registra_cassa(fattura_id, fattura, importo)
    elif mp in MP_BANCA:
        return await _registra_banca(fattura_id, fattura, importo, is_assegno=False)
    elif mp in MP_ASSEGNO:
        return await _registra_banca(fattura_id, fattura, importo, is_assegno=True)
    else:
        motivo = f"MP{mp}-sconosciuto" if mp else "metodo-mancante"
        return await _registra_provvisorio(fattura_id, fattura, importo, motivo)


async def _registra_cassa(fattura_id: str, fattura: dict, importo: float) -> dict:
    doc = {
        "_id":           _nuovo_id(),
        "fattura_id":    fattura_id,
        "data":          fattura.get("data_fattura", ""),
        "tipo":          "uscita",
        "importo":       abs(importo),
        "segno":         1.0 if importo >= 0 else -1.0,
        "descrizione":   f"Fattura {fattura.get('numero_fattura','')} — {fattura.get('fornitore_nome','')}",
        "categoria":     fattura.get("categoria_iva", "generico"),
        "fornitore_piva": fattura.get("fornitore_piva", ""),
        "tipo_documento": fattura.get("tipo_documento", ""),
        "source":        "auto",
        "anno":          fattura.get("anno", 0),
        "status":        "active",
        "created_at":    datetime.utcnow().isoformat(),
    }
    await col_pn_cassa().insert_one(doc)
    await _aggiorna_fattura_prima_nota(fattura_id, doc["_id"], "cassa")
    return {"destinazione": "cassa", "movimento_id": doc["_id"], "motivo": "MP01-contanti"}


async def _registra_banca(
    fattura_id: str, fattura: dict, importo: float, is_assegno: bool
) -> dict:
    doc = {
        "_id":           _nuovo_id(),
        "fattura_id":    fattura_id,
        "data":          fattura.get("data_fattura", ""),
        "tipo":          "uscita",
        "importo":       abs(importo),
        "segno":         1.0 if importo >= 0 else -1.0,
        "descrizione":   f"Fattura {fattura.get('numero_fattura','')} — {fattura.get('fornitore_nome','')}",
        "categoria":     fattura.get("categoria_iva", "generico"),
        "fornitore_piva": fattura.get("fornitore_piva", ""),
        "tipo_documento": fattura.get("tipo_documento", ""),
        "is_assegno":    is_assegno,
        "riconciliato":  False,
        "estratto_conto_id": None,
        "source":        "auto",
        "anno":          fattura.get("anno", 0),
        "status":        "active",
        "created_at":    datetime.utcnow().isoformat(),
    }
    await col_pn_banca().insert_one(doc)
    await _aggiorna_fattura_prima_nota(fattura_id, doc["_id"], "banca")

    dest = "assegno" if is_assegno else "banca"
    motivo = "MP08-assegno" if is_assegno else "MP02-bonifico"
    return {"destinazione": dest, "movimento_id": doc["_id"], "motivo": motivo}


async def _registra_provvisorio(
    fattura_id: str, fattura: dict, importo: float, motivo: str
) -> dict:
    doc = {
        "_id":             _nuovo_id(),
        "fattura_id":      fattura_id,
        "data":            fattura.get("data_fattura", ""),
        "importo":         abs(importo),
        "fornitore_nome":  fattura.get("fornitore_nome", ""),
        "fornitore_piva":  fattura.get("fornitore_piva", ""),
        "tipo_documento":  fattura.get("tipo_documento", ""),
        "iva_importo":     fattura.get("importo_iva", 0.0),
        "motivo_provvisorio": motivo,
        "stato":           "pending",
        "destinazione":    None,
        "confermato_in_id": None,
        "anno":            fattura.get("anno", 0),
        "created_at":      datetime.utcnow().isoformat(),
    }
    await col_pn_provvisori().insert_one(doc)
    await _aggiorna_fattura_prima_nota(fattura_id, doc["_id"], "provvisorio")
    return {"destinazione": "provvisorio", "movimento_id": doc["_id"], "motivo": motivo}


async def conferma_provvisorio(
    provvisorio_id: str,
    destinazione: str,        # "cassa" | "banca"
    data_pagamento: str = "",
    salva_metodo_fornitore: bool = False,
) -> dict:
    """
    Sposta un provvisorio in cassa o banca.
    Opzionalmente aggiorna il metodo pagamento del fornitore per il futuro.
    """
    prov = await col_pn_provvisori().find_one({"_id": provvisorio_id})
    if not prov:
        raise ValueError(f"Provvisorio {provvisorio_id} non trovato")
    if prov.get("stato") == "confermato":
        raise ValueError("Provvisorio già confermato")

    fattura_id = prov["fattura_id"]
    fattura    = await col_invoices().find_one({"_id": fattura_id})
    if not fattura:
        raise ValueError(f"Fattura {fattura_id} non trovata")

    data = data_pagamento or prov["data"]
    importo = prov["importo"]

    if destinazione == "cassa":
        mov_doc = {
            "_id":           _nuovo_id(),
            "fattura_id":    fattura_id,
            "data":          data,
            "tipo":          "uscita",
            "importo":       importo,
            "segno":         1.0,
            "descrizione":   f"Fattura {fattura.get('numero_fattura','')} — {prov['fornitore_nome']}",
            "categoria":     fattura.get("categoria_iva", "generico"),
            "fornitore_piva": prov["fornitore_piva"],
            "tipo_documento": prov.get("tipo_documento", ""),
            "source":        "conferma_provvisorio",
            "anno":          prov.get("anno", 0),
            "status":        "active",
            "created_at":    datetime.utcnow().isoformat(),
        }
        await col_pn_cassa().insert_one(mov_doc)
        coll_dest = "prima_nota_cassa"
        mp_new = "MP01"
    else:
        mov_doc = {
            "_id":           _nuovo_id(),
            "fattura_id":    fattura_id,
            "data":          data,
            "tipo":          "uscita",
            "importo":       importo,
            "segno":         1.0,
            "descrizione":   f"Fattura {fattura.get('numero_fattura','')} — {prov['fornitore_nome']}",
            "categoria":     fattura.get("categoria_iva", "generico"),
            "fornitore_piva": prov["fornitore_piva"],
            "tipo_documento": prov.get("tipo_documento", ""),
            "is_assegno":    False,
            "riconciliato":  False,
            "estratto_conto_id": None,
            "source":        "conferma_provvisorio",
            "anno":          prov.get("anno", 0),
            "status":        "active",
            "created_at":    datetime.utcnow().isoformat(),
        }
        await col_pn_banca().insert_one(mov_doc)
        coll_dest = "prima_nota_banca"
        mp_new = "MP02"

    # Aggiorna il provvisorio
    await col_pn_provvisori().update_one(
        {"_id": provvisorio_id},
        {"$set": {
            "stato":            "confermato",
            "destinazione":     destinazione,
            "confermato_in_id": mov_doc["_id"],
            "confermato_at":    datetime.utcnow().isoformat(),
        }}
    )

    # Aggiorna fattura
    await _aggiorna_fattura_prima_nota(fattura_id, mov_doc["_id"], destinazione)

    # Salva metodo sul fornitore per uso futuro
    if salva_metodo_fornitore and prov.get("fornitore_piva"):
        await col_fornitori().update_one(
            {"partita_iva": prov["fornitore_piva"]},
            {"$set": {"metodo_pagamento": mp_new}}
        )

    return {
        "movimento_id": mov_doc["_id"],
        "destinazione": destinazione,
        "collection":   coll_dest,
    }


def _col_by_name(nome: str):
    if nome == "cassa":      return col_pn_cassa()
    if nome == "banca":      return col_pn_banca()
    if nome == "provvisori": return col_pn_provvisori()
    raise ValueError(f"Collezione sconosciuta: {nome}")


async def sposta_movimento(
    movimento_id: str,
    da: str,   # "cassa" | "banca" | "provvisori"
    a: str,    # "cassa" | "banca" | "provvisori"
) -> dict:
    """Sposta un movimento tra cassa, banca e provvisori."""
    col_da = _col_by_name(da)
    col_a  = _col_by_name(a)

    doc = await col_da.find_one({"_id": movimento_id})
    if not doc:
        raise ValueError(f"Movimento {movimento_id} non trovato in {da}")

    # Soft delete nella sorgente
    await col_da.update_one(
        {"_id": movimento_id},
        {"$set": {"status": "spostato", "spostato_in": a,
                  "spostato_at": datetime.utcnow().isoformat()}}
    )

    # Inserisci nella destinazione con nuovo ID
    doc["_id"]        = _nuovo_id()
    doc["source"]     = f"spostato_da_{da}"
    doc["created_at"] = datetime.utcnow().isoformat()
    if a == "banca":
        doc.setdefault("is_assegno", False)
        doc.setdefault("riconciliato", False)
        doc.setdefault("estratto_conto_id", None)
    if a == "provvisori":
        doc["stato"] = "pending"

    await col_a.insert_one(doc)

    # Aggiorna fattura collegata
    if doc.get("fattura_id"):
        await _aggiorna_fattura_prima_nota(doc["fattura_id"], doc["_id"], a)

    return {"nuovo_movimento_id": doc["_id"], "destinazione": a}


async def _aggiorna_fattura_prima_nota(
    fattura_id: str, movimento_id: str, tipo: str
):
    stato = "pagata" if tipo in ("cassa", "banca") else "da_pagare"
    await col_invoices().update_one(
        {"_id": fattura_id},
        {"$set": {
            "prima_nota_id":   movimento_id,
            "prima_nota_tipo": tipo,
            "stato":           stato,
        }}
    )


async def _trova_movimento_esistente(fattura_id: str) -> Optional[dict]:
    """Cerca se esiste già un movimento attivo per questa fattura."""
    for col, dest in [
        (col_pn_cassa(),      "cassa"),
        (col_pn_banca(),      "banca"),
        (col_pn_provvisori(), "provvisorio"),
    ]:
        doc = await col.find_one({"fattura_id": fattura_id, "status": {"$ne": "deleted"}})
        if doc:
            return {"destinazione": dest, "movimento_id": doc["_id"]}
    return None
