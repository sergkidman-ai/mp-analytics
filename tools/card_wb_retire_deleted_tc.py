#!/usr/bin/env python3
# поток: card
"""Retire ONLY the 23 cards explicitly authorized by the user on 05.10.2026.

Default: live read-only preview. --apply zeros FBS and transfers to trash.
No TK, TN VED, other-account, DB or permanent deletion writes.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from collectors.wb import _content_token, _token

SOURCE = ROOT / "docs/reports/wb_tnved_dry_run_2026-10-05/no_source.csv"
OUT = ROOT / "docs/reports/wb_retire_deleted_tc_23_2026-10-05"
CONTENT = "https://content-api.wildberries.ru/content/v2"
MP = "https://marketplace-api.wildberries.ru/api/v3"
ALLOWED = {181115748, 199334972, 209763319, 215243864, 215243867, 215243869,
           215243879, 215243880, 215243886, 215243887, 215243895, 215243898,
           215243902, 209763326, 215243930, 209763338, 209763348, 209763361,
           209763364, 209763366, 209763374, 209763389, 181116714}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def journal(entry):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "journal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), **entry}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def api(method, url, headers, body=None, mutation=False):
    for attempt in range(4):
        if mutation:
            journal({"phase": "before_request", "method": method, "url": url, "body": body})
        response = requests.request(method, url, headers=headers, json=body, timeout=45)
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"text": response.text}
        if mutation:
            journal({"phase": "response", "method": method, "url": url,
                     "http": response.status_code, "response": data})
        if response.status_code == 429:
            delay = min(45, max(2, int(response.headers.get("Retry-After", "20"))))
            print("WB rate limit; retry in", delay, "seconds", flush=True)
            time.sleep(delay)
            continue
        response.raise_for_status()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError("WB returned error=true; see operation journal")
        time.sleep(.7)
        return data
    raise RuntimeError("WB rate limit retries exhausted")


def targets():
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    if len(rows) != 23 or {int(row["Артикул ВБ"]) for row in rows} != ALLOWED:
        raise ValueError("Source no longer matches the exact 23 authorized nmIDs")
    if any(row["account"] != "wb_acc1" or row["dry_run_status"] != "NO_SOURCE" for row in rows):
        raise ValueError("Unauthorized account or target status")
    return {int(row["Артикул ВБ"]): row["Внешний код карточки"] for row in rows}


def headers():
    content = _content_token("wb_acc1")
    marketplace = os.getenv("WB_TOKEN_RETURNS_ACC1")
    if not content or not marketplace:
        raise ValueError("Required WB tokens missing")
    return ({"Authorization": content, "Content-Type": "application/json"},
            {"Authorization": marketplace, "Content-Type": "application/json"})


def find_card(nm, h, trash=False):
    endpoint = CONTENT + ("/get/cards/trash" if trash else "/get/cards/list")
    body = {"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm), "withPhoto": -1}}}
    data = api("POST", endpoint, h, body)
    if not isinstance(data.get("cards"), list):
        raise ValueError("WB response has no cards array")
    found = [card for card in data["cards"] if card.get("nmID") == nm]
    if len(found) > 1:
        raise ValueError("WB returned duplicate target")
    return found[0] if found else None


def read_cards(expected, h, verification=False):
    cards, states = {}, {}
    if verification:
        # Trash textSearch is unreliable for individual nmIDs. Read its actual
        # paginated membership and intersect with the exact authorized list.
        trash, cursor, visited = {}, {"limit": 100}, set()
        while True:
            body = {"settings": {"sort": {"ascending": False}, "cursor": cursor}}
            data = api("POST", CONTENT + "/get/cards/trash", h, body)
            page = data.get("cards")
            if not isinstance(page, list):
                raise ValueError("WB trash response has no cards array")
            for card in page:
                if card.get("nmID") in expected:
                    trash[card["nmID"]] = card
            if len(trash) == len(expected) or len(page) < 100:
                break
            cur = data.get("cursor") or {}
            key = (cur.get("trashedAt"), cur.get("nmID"))
            if not all(key) or key in visited:
                raise ValueError("Trash pagination did not advance")
            visited.add(key)
            cursor = {"limit": 100, "trashedAt": key[0], "nmID": key[1]}
        for nm, code in expected.items():
            active = find_card(nm, h)
            card = active or trash.get(nm)
            if card is None or card.get("vendorCode") != code:
                raise ValueError(f"Target {nm}: identity cannot be confirmed")
            if active and nm in trash:
                raise ValueError(f"Target {nm}: appears in both active and trash lists")
            cards[nm], states[nm] = card, "active" if active else "trash"
        return cards, states
    for nm, code in expected.items():
        active = find_card(nm, h)
        trash = find_card(nm, h, trash=True) if active is None else None
        card = active or trash
        if card is None or card.get("vendorCode") != code:
            raise ValueError(f"Target {nm}: identity cannot be confirmed")
        if active and trash:
            raise ValueError(f"Target {nm}: appears in both active and trash lists")
        cards[nm] = card
        states[nm] = "active" if active else "trash"
    return cards, states


def stock_ids(cards):
    ids = [size["chrtID"] for card in cards.values() for size in card.get("sizes", [])]
    if not ids or len(ids) != len(set(ids)) or any(not card.get("sizes") for card in cards.values()):
        raise ValueError("Missing or duplicate target size IDs")
    return ids


def read_fbs(ids, h, warehouses=None):
    warehouses = warehouses if warehouses is not None else api("GET", MP + "/warehouses", h)
    if not isinstance(warehouses, list) or not warehouses:
        raise ValueError("Seller warehouses could not be confirmed")
    stocks = {}
    for warehouse in warehouses:
        data = api("POST", MP + f"/stocks/{warehouse['id']}", h, {"chrtIds": ids})
        if not isinstance(data.get("stocks"), list):
            raise ValueError("WB stock response missing stocks array")
        values = {row["chrtId"]: row["amount"] for row in data["stocks"]}
        if any(ident not in ids for ident in values):
            raise ValueError("Unexpected stock ID in response")
        stocks[warehouse["id"]] = {ident: values.get(ident) for ident in ids}
    return warehouses, stocks


def fbo_preview():
    token = os.getenv("WB_TOKEN_ANALYTICS_ACC1") or _token("wb_acc1")
    data = api("POST", "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
               {"Authorization": token, "Content-Type": "application/json"},
               {"nmIds": sorted(ALLOWED), "limit": 1000, "offset": 0})
    items = (data.get("data") or {}).get("items")
    if not isinstance(items, list) or len(items) >= 1000:
        raise ValueError("FBO stock response incomplete")
    if any(row.get("nmId") not in ALLOWED for row in items):
        raise ValueError("Unexpected article in FBO response")
    return {"read_at": now(), "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.apply and args.verify:
        parser.error("Use only one mode")
    expected = targets()
    hc, hm = headers()
    cards, states = read_cards(expected, hc, verification=args.verify)
    ids = stock_ids(cards)
    warehouses, stocks = read_fbs(ids, hm)
    if args.verify:
        result = {"at": now(), "total": len(cards), "trash": sum(s == "trash" for s in states.values()),
                  "active": sum(s == "active" for s in states.values()),
                  "positive_stock_cells": sum(q > 0 for w in stocks.values() for q in w.values() if q is not None),
                  "unknown_stock_cells": sum(q is None for w in stocks.values() for q in w.values()),
                  "warehouses": len(warehouses), "states": states, "stocks": stocks}
        save("verification.json", result)
        save("cards_after.json", cards)
        print(json.dumps({k: v for k, v in result.items() if k not in ("stocks", "states")}, ensure_ascii=False), flush=True)
        if result["active"] or result["positive_stock_cells"] or result["unknown_stock_cells"]:
            raise RuntimeError("Verification incomplete; inspect saved result")
        return
    save("cards_before.json" if not args.apply else "cards_before_apply.json", cards)
    save("fbs_before.json" if not args.apply else "fbs_before_apply.json",
         {"at": now(), "warehouses": warehouses, "stocks": stocks})
    if not args.apply:
        try:
            fbo = fbo_preview()
            save("fbo_before.json", fbo)
            fbo_quantity = sum(row.get("quantity", 0) for row in fbo["items"])
        except Exception as error:
            fbo_quantity = None
            save("fbo_read_error.json", {"at": now(), "error_type": type(error).__name__, "message": str(error)})
        result = {"at": now(), "cards": len(cards), "size_ids": len(ids), "warehouses": len(warehouses),
                  "active": sum(s == "active" for s in states.values()),
                  "fbs_quantity": sum(q for w in stocks.values() for q in w.values() if q is not None),
                  "unknown_stock_cells": sum(q is None for w in stocks.values() for q in w.values()),
                  "fbo_quantity": fbo_quantity, "marketplace_writes": 0}
        save("preview.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    # Fresh read above binds size IDs to the exact, authorized nmIDs/vendorCodes.
    for warehouse in warehouses:
        wh = warehouse["id"]
        pending = [ident for ident in ids if stocks[wh][ident] != 0]
        if not pending:
            continue
        api("PUT", MP + f"/stocks/{wh}", hm,
            {"stocks": [{"chrtId": ident, "amount": 0} for ident in pending]}, mutation=True)
        _, check = read_fbs(ids, hm, [warehouse])
        save(f"fbs_after_zero_{wh}.json", {"at": now(), "stocks": check})
        if any(check[wh][ident] != 0 for ident in ids):
            raise RuntimeError("FBS zeros not explicitly confirmed; stop before trash")
        print("FBS warehouse zero confirmed:", wh, "size IDs:", len(ids), flush=True)
    # Re-read all warehouses, including any added since the preview.
    current_warehouses, check = read_fbs(ids, hm)
    save("fbs_after_zero_all.json", {"at": now(), "warehouses": current_warehouses, "stocks": check})
    if any(amount != 0 for values in check.values() for amount in values.values()):
        raise RuntimeError("Some FBS stock remains or is unknown; stop before trash")
    pending_nm = [nm for nm, state in states.items() if state == "active"]
    if pending_nm:
        api("POST", CONTENT + "/cards/delete/trash", hc, {"nmIDs": pending_nm}, mutation=True)
    save("apply_response.json", {"at": now(), "fbs_zero_confirmed": True, "trash_requested": pending_nm,
                                  "verification_pending": True})
    print("Trash request accepted for", len(pending_nm), "cards; verify by rereading", flush=True)


if __name__ == "__main__":
    main()
