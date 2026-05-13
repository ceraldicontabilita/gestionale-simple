"""
Auto-match Assegni ↔ Fatture — Ceraldi Group ERP
4 livelli di matching con tolleranza ±0.005€:
  L1: 1 assegno ↔ 1 fattura (importo esatto)
  L2: N assegni uguali ↔ 1 fattura (somma = importo)
  L3: N assegni diversi ↔ 1 fattura (somma = importo)
  L4: 1 assegno ↔ N fatture (importo = somma)
"""
from datetime import datetime, timezone

TOL = 0.005


def _amt_eq(a: float, b: float) -> bool:
    return abs(a - b) <= TOL


async def run_auto_match(db, dry_run: bool = False) -> dict:
    assegni_coll  = db["assegni"]
    invoices_coll = db["invoices"]

    # Carica assegni da abbinare (compilati/emessi, senza fattura collegata)
    assegni_raw = await assegni_coll.find({
        "stato":             {"$in": ["compilato", "emesso"]},
        "fattura_collegata": {"$in": [None, ""]},
        "importo":           {"$exists": True, "$ne": None},
        "entity_status":     {"$ne": "deleted"},
    }).to_list(length=None)

    # Carica fatture da pagare
    fatture_raw = await invoices_coll.find({
        "stato": "da_pagare",
    }).to_list(length=None)

    # Indici
    fatture_per_piva: dict[str, list] = {}
    for f in fatture_raw:
        piva = f.get("fornitore_piva", "")
        fatture_per_piva.setdefault(piva, []).append(f)

    assegni_per_piva: dict[str, list] = {}
    for a in assegni_raw:
        piva = a.get("fornitore_piva", "")
        assegni_per_piva.setdefault(piva, []).append(a)

    match_l1, match_l2, match_l3, match_l4 = [], [], [], []
    ambigui:     list = []
    non_trovati: list = []

    used_assegni:  set = set()
    used_fatture:  set = set()

    async def _link(ass_ids: list, fatt_ids: list, livello: str):
        if dry_run:
            return
        for aid in ass_ids:
            await assegni_coll.update_one(
                {"_id": aid},
                {"$set": {
                    "fattura_collegata": fatt_ids[0] if len(fatt_ids) == 1 else fatt_ids,
                    "stato":             "compilato",
                    "updated_at":        datetime.now(timezone.utc).isoformat(),
                }}
            )
        for fid in fatt_ids:
            await invoices_coll.update_one(
                {"_id": fid},
                {"$set": {
                    "assegno_match_livello": livello,
                    "updated_at":           datetime.now(timezone.utc).isoformat(),
                }}
            )

    all_pive = set(assegni_per_piva) | set(fatture_per_piva)

    for piva in all_pive:
        ass_list  = [a for a in assegni_per_piva.get(piva, []) if str(a["_id"]) not in used_assegni]
        fatt_list = [f for f in fatture_per_piva.get(piva, []) if str(f["_id"]) not in used_fatture]

        if not ass_list or not fatt_list:
            for a in ass_list:
                non_trovati.append({"assegno": str(a["_id"]), "numero": a.get("numero"), "importo": a.get("importo")})
            continue

        # ── L1: 1:1 exact ────────────────────────────────────────────────────
        for a in list(ass_list):
            if str(a["_id"]) in used_assegni:
                continue
            amt_a = float(a.get("importo") or 0)
            candidates = [f for f in fatt_list
                          if str(f["_id"]) not in used_fatture
                          and _amt_eq(float(f.get("importo_totale") or 0), amt_a)]
            if len(candidates) == 1:
                f = candidates[0]
                match_l1.append({"assegno": str(a["_id"]), "fattura": str(f["_id"]), "importo": amt_a})
                used_assegni.add(str(a["_id"]))
                used_fatture.add(str(f["_id"]))
                await _link([a["_id"]], [f["_id"]], "L1")
            elif len(candidates) > 1:
                ambigui.append({"assegno": str(a["_id"]), "candidati": [str(c["_id"]) for c in candidates]})

        # ── L2: N assegni uguali : 1 fattura ─────────────────────────────────
        remaining_ass  = [a for a in ass_list  if str(a["_id"]) not in used_assegni]
        remaining_fatt = [f for f in fatt_list if str(f["_id"]) not in used_fatture]

        for f in list(remaining_fatt):
            if str(f["_id"]) in used_fatture:
                continue
            amt_f = float(f.get("importo_totale") or 0)
            # Raggruppa per importo uguale
            by_amt: dict[float, list] = {}
            for a in remaining_ass:
                if str(a["_id"]) in used_assegni:
                    continue
                k = round(float(a.get("importo") or 0), 2)
                by_amt.setdefault(k, []).append(a)

            for unit_amt, group in by_amt.items():
                if unit_amt <= 0:
                    continue
                n = round(amt_f / unit_amt)
                if n >= 2 and len(group) >= n and _amt_eq(n * unit_amt, amt_f):
                    chosen = group[:n]
                    aids = [a["_id"] for a in chosen]
                    match_l2.append({
                        "assegni": [str(x) for x in aids],
                        "fattura": str(f["_id"]),
                        "n": n,
                        "importo_unitario": unit_amt,
                    })
                    for aid in aids:
                        used_assegni.add(str(aid))
                    used_fatture.add(str(f["_id"]))
                    await _link(aids, [f["_id"]], "L2")
                    break

        # ── L3: N assegni diversi : 1 fattura ────────────────────────────────
        remaining_ass  = [a for a in ass_list  if str(a["_id"]) not in used_assegni]
        remaining_fatt = [f for f in fatt_list if str(f["_id"]) not in used_fatture]

        for f in list(remaining_fatt):
            if str(f["_id"]) in used_fatture:
                continue
            amt_f = float(f.get("importo_totale") or 0)
            # Subset sum greedy (max 6 assegni per evitare esplosione combinatoria)
            candidates = sorted(
                [a for a in remaining_ass if str(a["_id"]) not in used_assegni],
                key=lambda a: float(a.get("importo") or 0),
                reverse=True,
            )[:12]
            found = _subset_sum(candidates, amt_f)
            if found and len(found) >= 2:
                aids = [a["_id"] for a in found]
                match_l3.append({
                    "assegni": [str(x) for x in aids],
                    "fattura": str(f["_id"]),
                    "importo": amt_f,
                })
                for aid in aids:
                    used_assegni.add(str(aid))
                used_fatture.add(str(f["_id"]))
                await _link(aids, [f["_id"]], "L3")

        # ── L4: 1 assegno : N fatture ─────────────────────────────────────────
        remaining_ass  = [a for a in ass_list  if str(a["_id"]) not in used_assegni]
        remaining_fatt = [f for f in fatt_list if str(f["_id"]) not in used_fatture]

        for a in list(remaining_ass):
            if str(a["_id"]) in used_assegni:
                continue
            amt_a = float(a.get("importo") or 0)
            candidates = sorted(
                [f for f in remaining_fatt if str(f["_id"]) not in used_fatture],
                key=lambda f: float(f.get("importo_totale") or 0),
                reverse=True,
            )[:12]
            found = _subset_sum(candidates, amt_a, key=lambda x: float(x.get("importo_totale") or 0))
            if found and len(found) >= 2:
                fids = [f["_id"] for f in found]
                match_l4.append({
                    "assegno":  str(a["_id"]),
                    "fatture":  [str(x) for x in fids],
                    "importo":  amt_a,
                })
                used_assegni.add(str(a["_id"]))
                for fid in fids:
                    used_fatture.add(str(fid))
                await _link([a["_id"]], fids, "L4")

        # Assegni rimasti senza match
        for a in ass_list:
            if str(a["_id"]) not in used_assegni:
                non_trovati.append({
                    "assegno": str(a["_id"]),
                    "numero":  a.get("numero"),
                    "importo": a.get("importo"),
                    "piva":    piva,
                })

    return {
        "match_l1":    match_l1,
        "match_l2":    match_l2,
        "match_l3":    match_l3,
        "match_l4":    match_l4,
        "ambigui":     ambigui,
        "non_trovati": non_trovati,
        "dry_run":     dry_run,
    }


def _subset_sum(items: list, target: float, key=None, depth: int = 0) -> list | None:
    """Greedy subset-sum: trova il sottoinsieme minimo che somma a target (±TOL)."""
    if key is None:
        key = lambda x: float(x.get("importo") or 0)
    if depth > 6:
        return None
    accum = 0.0
    chosen = []
    for item in items:
        v = key(item)
        if accum + v <= target + TOL:
            accum += v
            chosen.append(item)
            if _amt_eq(accum, target):
                return chosen
    return None
