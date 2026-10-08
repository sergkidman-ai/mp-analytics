#!/usr/bin/env python3
# поток: mkt
"""Read-only preparation of WB bundle grouping. Never calls moveNm or writes DB.

Run: ./venv/bin/python -m ops.wb_bundle_plan --refresh
Produces per-account review CSVs, proposed requests, source-group snapshots and
a 3351 pilot preview. Plans are NOT an authorization or an executable batch.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

import requests

from collectors.wb import CARDS_URL, _content_token
from core.db import query
from ops.wb_chip_map import chip_of

ROOT = Path(__file__).resolve().parents[1]
GROUP_LIMIT = 30  # Conservative preparation ceiling; recheck before execution.
ACCOUNTS = ("wb_acc1", "wb_acc2")


def fetch_catalog(account, text_search=None):
    """Read every active card via Content API; no reliance on blocked public API."""
    headers = {"Authorization": _content_token(account), "Content-Type": "application/json"}
    indexed, cursor, cursors, repeated = {}, {"limit": 100}, set(), 0
    while True:
        filters = {"withPhoto": -1}
        if text_search is not None:
            filters["textSearch"] = str(text_search)
        body = {"settings": {"sort": {"ascending": True}, "cursor": cursor, "filter": filters}}
        for attempt in range(5):
            response = requests.post(CARDS_URL, headers=headers, json=body, timeout=45)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(30, max(2, int(response.headers.get("Retry-After", "3")))))
                continue
            response.raise_for_status()
            data = response.json()
            break
        else:
            raise RuntimeError(f"{account}: Content API retries exhausted")
        batch = data.get("cards")
        if not isinstance(batch, list):
            raise RuntimeError(f"{account}: missing cards in Content API response")
        for card in batch:
            nm = card.get("nmID")
            if not nm:
                raise RuntimeError(f"{account}: missing nmID in paginated response")
            # Catalog updates while pagination runs can move a card ahead of the
            # cursor. Keep its latest observed representation, not a duplicate.
            if nm in indexed:
                repeated += 1
            indexed[nm] = card
        if len(indexed) % 5000 == 0:
            print(account, "active cards read", len(indexed), flush=True)
        if len(batch) < 100:
            if repeated:
                print(account, "repeated observations during live pagination", repeated, flush=True)
            return list(indexed.values())
        cur = data.get("cursor") or {}
        key = (cur.get("updatedAt"), cur.get("nmID"))
        if not all(key) or key in cursors:
            raise RuntimeError(f"{account}: pagination cursor did not advance")
        cursors.add(key)
        cursor = {"limit": 100, "updatedAt": key[0], "nmID": key[1]}
        time.sleep(0.65)


def char(card, ident):
    value = next((x.get("value") for x in card.get("characteristics", [])
                  if x.get("id") == ident), None)
    if isinstance(value, list):
        return "; ".join(map(str, value))
    return str(value) if value is not None else ""


def number(value):
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def normalized(value):
    return re.sub(r"[\s\-–—№]+", "", str(value).upper().replace("Ё", "Е"))


def color(value):
    text = normalized(value)
    aliases = {"BK": "K", "BLACK": "K", "ЧЕРНЫЙ": "K", "ЧЕРНЫЙЦВЕТ": "K",
               "ГОЛУБОЙ": "C", "CYAN": "C", "ЖЕЛТЫЙ": "Y", "YELLOW": "Y",
               "ПУРПУРНЫЙ": "M", "MAGENTA": "M", "ФОТОЧЕРНЫЙ": "PBK",
               "PHOTOBLACK": "PBK", "MATTEBLACK": "MBK", "МАТОВЫЙЧЕРНЫЙ": "MBK"}
    return aliases.get(text, text)


def quantities(card):
    field = number(char(card, 179792))
    match = re.search(r"\b(\d+)\s*шт", card.get("title", ""), re.I)
    title = int(match[1]) if match else None
    code = re.match(r"^\d{4}[XХ](\d+)", card.get("vendorCode", ""), re.I)
    code_qty = int(code[1]) if code else None
    qty = title if title is not None else field
    return qty, field, title, code_qty


def is_bundle(card):
    _, field, title, code_qty = quantities(card)
    if code_qty is None or code_qty <= 1:
        return False
    code = card.get("vendorCode", "")
    if re.fullmatch(r"\d{4}[XХ]\d{1,2}", code, re.I):
        # Несогласованная кратность точного XN остаётся кандидатом для ручного review.
        return any(n is not None and n > 1 for n in (field, title))
    from ops.wb_promo_guard import bundle_multiplier
    return bundle_multiplier(code, title=card.get("title")) > 1


def build_references():
    tc = {x["external_code"]: x for x in query(
        "select external_code,title,color,resource,chip,consumable_type from prc_tc_model where gone_at is null")}
    names, idcode = defaultdict(list), {}
    for row in query("select ms_id,external_code,name from ms_product where external_code is not null"):
        names[row["external_code"]].append(row["name"] or "")
        idcode[row["ms_id"]] = row["external_code"]
    for row in query("select distinct ms_id,name from prc_price_row where ms_id is not null"):
        code = idcode.get(row["ms_id"])
        if code:
            names[code].append(row["name"] or "")
    chips = {}
    for code in tc.keys() | names.keys():
        marks = {chip_of(n) for n in names.get(code, [])} - {None}
        tk = {"chip": "с чипом", "nochip": "без чипа", "chip_free": "чип без счётчика"}.get(tc.get(code, {}).get("chip"))
        if len(marks) > 1:
            chips[code] = ("противоречие", "МС/прайсы: разные классы чипа")
        elif marks:
            chips[code] = (next(iter(marks)), "явное указание в МС/прайсах")
        elif tk:
            chips[code] = (tk, "ТК: заполненное поле чипа")
        elif len(names.get(code, [])) >= 3:
            chips[code] = ("без чипа", "правило 22.09: >=3 товаров МС/прайсов без указания чипа")
        else:
            chips[code] = ("?", "недостаточно данных")
    return tc, chips


def issues(card, parent, reference, chip_reference):
    reasons = []
    qty, field, title, code_qty = quantities(card)
    if any(n is not None and n != qty for n in (field, title, code_qty)):
        reasons.append("количество: код/название/характеристика расходятся")
    if not reference:
        reasons.append("нет модели в актуальном ТК")
    elif "набор" in (reference.get("consumable_type") or "").lower():
        reasons.append("ТК: набор, не подтвержден бандл одинаковых изделий")
    if parent.get("subjectID") != card.get("subjectID"):
        reasons.append("разные предметы ВБ")
    if not card.get("imtID") or not parent.get("imtID"):
        reasons.append("не определена группа ВБ")
    models = [normalized(char(c, 5023)) for c in (parent, card)]
    if not all(models):
        reasons.append("не заполнена модель ВБ")
    elif models[0] != models[1]:
        reasons.append("модель бандла отличается от основной")
    elif reference and models[0] != normalized(reference.get("title") or ""):
        reasons.append("модель ВБ отличается от ТК")
    colors = [color(char(c, 58854)) for c in (parent, card)]
    if not all(colors):
        reasons.append("не заполнен цвет ВБ")
    elif colors[0] != colors[1]:
        reasons.append("цвет бандла отличается от основной")
    elif reference and colors[0] != color(reference.get("color") or ""):
        reasons.append("цвет ВБ отличается от ТК")
    resources = [number(char(c, 88963)) for c in (parent, card)]
    if None in resources or not all(resources):
        reasons.append("не заполнен ресурс ВБ")
    elif resources[0] != resources[1]:
        reasons.append("ресурс: нужна проверка единичного и суммарного значения")
    elif reference and resources[0] != reference.get("resource"):
        reasons.append("ресурс ВБ отличается от ТК")
    expected, _ = chip_reference
    if expected in ("?", "противоречие"):
        reasons.append("чип: " + expected)
    for c in (parent, card):
        for text in (char(c, 378533), c.get("description", "")):
            actual = chip_of(text)
            if actual and expected not in ("?", "противоречие") and actual != expected:
                reasons.append("чип в карточке противоречит МС/ТК")
    tk_chip = {"chip": "с чипом", "nochip": "без чипа", "chip_free": "чип без счётчика"}.get((reference or {}).get("chip"))
    if tk_chip and expected not in ("?", "противоречие") and tk_chip != expected:
        reasons.append("чип МС/прайсов противоречит ТК")
    return sorted(set(reasons))


def fingerprint(cards):
    """A full group fingerprint for later preflight; ignores volatile update times."""
    selected = [{k: c.get(k) for k in ("nmID", "imtID", "vendorCode", "subjectID", "title", "characteristics", "description")}
                for c in sorted(cards, key=lambda c: c["nmID"])]
    for c in selected:
        c["characteristics"] = sorted(c.get("characteristics") or [], key=lambda x: (x.get("id", 0), json.dumps(x, sort_keys=True)))
    return hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_preview(operation, cards):
    """Reject stale plans, missing cards and changed source/target group members."""
    expected_groups = {int(g) for g in operation["before_groups"]}
    current = [c for c in cards if c.get("imtID") in expected_groups]
    if fingerprint(current) != operation["source_fingerprint"]:
        return {"ok": False, "reason": "source/target groups or relevant product fields changed; rebuild preview"}
    return {"ok": True, "cards_checked": len(current)}


def verify_result(operation, cards):
    """Verify desired grouping after a separately authorized move, without writes."""
    indexed = {c["nmID"]: c for c in cards}
    expected = operation["body_preview"]["targetIMT"]
    ids = set(operation["body_preview"]["nmIDs"]) | {operation["target_nm"]}
    missing = sorted(ids - indexed.keys())
    mismatches = sorted(n for n in ids & indexed.keys() if indexed[n].get("imtID") != expected)
    return {"ok": not missing and not mismatches, "expected_imt": expected,
            "missing_nm_ids": missing, "different_group_nm_ids": mismatches}


def plan_account(account, cards, tc, chips):
    groups, parents, rows = defaultdict(list), defaultdict(list), []
    bundle_ids = defaultdict(set)
    for c in cards:
        groups[c.get("imtID")].append(c)
        code = c.get("vendorCode", "")
        if is_bundle(c):
            bundle_ids[code[:4]].add(c["nmID"])
        if re.fullmatch(r"\d{4}", code) or (account == "wb_acc2" and re.fullmatch(r"\d{4}[A-Z0-9]{8}", code) and not is_bundle(c)):
            if quantities(c)[0] == 1:
                parents[code[:4]].append(c)
    families = defaultdict(list)
    for c in cards:
        if not is_bundle(c):
            continue
        code = c["vendorCode"]; base = code[:4]; pa = parents.get(base, [])
        candidates = pa
        # Diskver's printer-specific articles sometimes have the same length as
        # the randomized primary article. Select by the actual TC model, never
        # by length/prefix alone; retain all alternatives for review.
        if len(pa) > 1 and tc.get(base):
            matching = [p for p in pa if normalized(char(p, 5023)) == normalized(tc[base].get("title") or "")]
            if len(matching) == 1:
                pa = matching
        parent = pa[0] if len(pa) == 1 else None
        reason = [] if parent else ["основная не найдена" if not pa else "несколько основных карточек"]
        chip = chips.get(base, ("?", "код не найден"))
        if parent:
            reason += issues(c, parent, tc.get(base), chip)
            # Moving one member of a useful source group needs explicit review.
            own_family = bundle_ids[base] | {parent["nmID"]}
            foreign = [x for x in groups[c["imtID"]] if x["nmID"] not in own_family]
            if foreign and c["imtID"] != parent["imtID"]:
                reason.append("в исходной группе есть другие карточки")
        row = {"account": account, "base_code": base, "bundle_code": code, "bundle_nm": c["nmID"],
               "quantity": quantities(c)[0], "source_imt": c.get("imtID"), "source_members": [x["nmID"] for x in groups[c.get("imtID")]],
               "parent_nm": parent["nmID"] if parent else None, "parent_code": parent.get("vendorCode") if parent else None,
               "parent_candidates": [{"nm": p["nmID"], "code": p.get("vendorCode"), "model": char(p, 5023)} for p in candidates],
               "target_imt": parent.get("imtID") if parent else None,
               "target_members": [x["nmID"] for x in groups[parent["imtID"]]] if parent else [],
               "model": char(c, 5023), "color": char(c, 58854), "resource": char(c, 88963),
               "chip": chip[0], "chip_source": chip[1], "reasons": sorted(set(reason)),
               "status": "review" if reason else ("already_linked" if c["imtID"] == parent["imtID"] else "candidate")}
        rows.append(row); families[base].append(row)
    operations = []
    for base, family in sorted(families.items()):
        movable = [r for r in family if r["status"] == "candidate"]
        if not movable:
            continue
        first = movable[0]; target = first["target_imt"]
        nm_ids = sorted(r["bundle_nm"] for r in movable)
        all_future = set(first["target_members"]) | set(nm_ids)
        if len(all_future) > GROUP_LIMIT:
            for r in movable:
                r["status"] = "review"; r["reasons"].append("итоговая группа больше консервативного предела 30")
            continue
        affected_groups = {target} | {r["source_imt"] for r in movable}
        affected = [c for g in affected_groups for c in groups[g]]
        operations.append({"account": account, "base_code": base, "target_nm": first["parent_nm"],
                           "body_preview": {"targetIMT": target, "nmIDs": nm_ids},
                           "future_group_size": len(all_future), "source_fingerprint": fingerprint(affected),
                           "before_groups": {str(g): [{"nmID": c["nmID"], "imtID": c.get("imtID"),
                                                        "vendorCode": c.get("vendorCode"), "subjectID": c.get("subjectID"),
                                                        "model": char(c, 5023), "color": char(c, 58854), "resource": char(c, 88963)}
                                                       for c in groups[g]] for g in sorted(affected_groups)},
                           "rollback_preview": [{"targetIMT": g, "nmIDs": sorted(r["bundle_nm"] for r in movable if r["source_imt"] == g)}
                                                for g in sorted(affected_groups - {target})],
                           "family_has_other_review_rows": any(r["status"] == "review" for r in family)})
    return rows, operations


def write_outputs(out, account, rows, operations):
    columns = list(rows[0]) if rows else ["account", "status"]
    with (out / f"{account}_review.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns); writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for k, v in row.items()})
    (out / f"{account}_operations_preview.json").write_text(json.dumps(operations, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Read full active catalogs from WB")
    parser.add_argument("--verify-preview", type=Path, help="Read active catalog and validate an existing preview, without changing it")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/reports/wb_bundle_plan_2026-10-04")
    args = parser.parse_args()
    if args.verify_preview:
        operations = json.loads(args.verify_preview.read_text())
        catalogs = {acc: fetch_catalog(acc) for acc in sorted({o["account"] for o in operations})}
        results = [{"account": o["account"], "base_code": o["base_code"],
                    **verify_preview(o, catalogs[o["account"]])} for o in operations]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(r["ok"] for r in results):
            raise SystemExit(1)
        return
    args.out.mkdir(parents=True, exist_ok=True)
    backup = ROOT / "backups/wb_bundle_plan_2026-10-04"; backup.mkdir(parents=True, exist_ok=True)
    if args.refresh:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {acc: pool.submit(fetch_catalog, acc) for acc in ACCOUNTS}
            for acc in ACCOUNTS:
                cards = futures[acc].result()
                (backup / f"{acc}_active.json").write_text(json.dumps({"read_at": datetime.now(timezone.utc).isoformat(), "cards": cards}, ensure_ascii=False))
                print(acc, "full live read complete", len(cards), flush=True)
    tc, chips = build_references(); summary = {}; pilots = {}
    for acc in ACCOUNTS:
        snapshot = json.loads((backup / f"{acc}_active.json").read_text())
        rows, operations = plan_account(acc, snapshot["cards"], tc, chips)
        write_outputs(args.out, acc, rows, operations)
        summary[acc] = {"read_at": snapshot["read_at"], "active_cards": len(snapshot["cards"]),
                        "bundles": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
                        "operations": len(operations), "reasons": dict(Counter(reason for r in rows for reason in r["reasons"]))}
        pilots[acc] = {"rows": [r for r in rows if r["base_code"] == "3351"],
                       "operations_preview": [o for o in operations if o["base_code"] == "3351"]}
        print(acc, json.dumps(summary[acc], ensure_ascii=False), flush=True)
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (args.out / "3351_pilot_preview.json").write_text(json.dumps(pilots, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
