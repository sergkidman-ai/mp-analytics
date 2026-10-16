#!/usr/bin/env python3
# поток: card
"""Apply owner's 05.10 rule ONLY to the exact 203 reviewed cards.

Subject 1820 + both fields empty -> 8443999000 in both fields.
Default is a read-only preview. Pilot verification gates batches.
"""
import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from collectors.wb import _content_token
from tools.card_wb_tnved_rule_recheck import fetch, char
from tools.card_wb_tnved_dryrun import pilot_request

SOURCE = ROOT / "docs/reports/wb_tnved_category_rule_recheck_2026-10-05/correct_category_code_conflicts.csv"
OUT = ROOT / "docs/reports/wb_tnved_apply_203_2026-10-05"
SCOPE_HASH = "f23633283e088b6cfe648fd6ecb6d2d82760c167926680e70f63a96c60a0bb25"
CODE = "8443999000"
FIELDS = (15000001, 15004139)
UPDATE = "https://content-api.wildberries.ru/content/v2/cards/update"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log(data):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "journal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), **data}, ensure_ascii=False) + "\n")
        handle.flush(); os.fsync(handle.fileno())


def scope():
    with SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    identities = sorted((r["account"], int(r["nmID"]), r["vendorCode"]) for r in rows)
    digest = hashlib.sha256(json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if len(rows) != 203 or digest != SCOPE_HASH:
        raise ValueError("Target scope no longer matches the exact authorized 203")
    return rows


def key(row):
    return f"{row['account']}_{row['nmID']}"


def preservation(card):
    # Preserve every observed value except the intended TN VED fields and
    # server timestamps/derived validity. Characteristic order is immaterial.
    result = deepcopy(card)
    result.pop("updatedAt", None)
    result["dimensions"].pop("isValid", None)
    result["characteristics"] = {str(c["id"]): c["value"] for c in card["characteristics"] if c["id"] not in FIELDS}
    if "documents" in result:
        # WB documents/listing verdicts are asynchronous service results, not
        # editable card content. Preserve the documents and exclusion flag.
        result["documents"].pop("overallVerdict", None)
        result["documents"].setdefault("items", [])
        for item in result["documents"]["items"]:
            item.pop("verdict", None)
    return result


def read_target(session, row):
    card = fetch(session, row["account"], int(row["nmID"]))
    if not card or card["vendorCode"] != row["vendorCode"]:
        raise ValueError("Exact card identity not confirmed")
    if card["subjectID"] != 1820:
        raise ValueError("Card left authorized subject 1820")
    return card


def send(session, account, body):
    log({"phase": "before_update", "account": account, "body": body})
    response = session.post(UPDATE, headers={"Authorization": _content_token(account)}, json=body, timeout=45)
    try:
        result = response.json() if response.content else {}
    except ValueError:
        result = {"text": response.text}
    log({"phase": "update_response", "account": account, "http": response.status_code, "response": result})
    response.raise_for_status()
    if result.get("error"):
        raise RuntimeError("WB rejected update; see journal")
    # Never blindly retry an uncertain mutation. Resume by fresh reading.


def verify(session, row, before, results, seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        after = read_target(session, row)
        save(f"after/{key(row)}.json", after)
        correct = all(char(after, ident) == CODE for ident in FIELDS)
        if correct:
            preserved = preservation(after) == preservation(before)
            if not preserved:
                changed = [k for k in preservation(before) if preservation(before).get(k) != preservation(after).get(k)]
                log({"phase": "preservation_failure", "account": row["account"], "nmID": int(row["nmID"]), "changed_keys": changed})
                raise RuntimeError("Non-TN-VED values changed; stop and inspect before/after")
            results[key(row)] = {"account": row["account"], "nmID": int(row["nmID"]), "vendorCode": row["vendorCode"],
                "status": "VERIFIED", "at": now(), "other_values_preserved": True,
                "document_validation_result_changed": before.get("documents", {}).get("overallVerdict") != after.get("documents", {}).get("overallVerdict"),
                "wholesale_enabled": bool(before.get("wholesale", {}).get("enabled"))}
            save("results.json", results)
            return
        if time.monotonic() >= deadline:
            log({"phase": "verification_pending", "account": row["account"], "nmID": int(row["nmID"])})
            raise RuntimeError("Update not yet confirmed; stop batches and resume with verification")
        time.sleep(4)


def apply_batch(session, rows, results):
    ready = []
    for row in rows:
        if key(row) in results and results[key(row)]["status"] == "VERIFIED":
            continue
        card = read_target(session, row)
        first, second = (char(card, ident) for ident in FIELDS)
        if first or second:
            original = OUT / "before" / f"{key(row)}.json"
            if first == second == CODE and original.exists():
                verify(session, row, json.loads(original.read_text()), results, seconds=1)
                continue
            # Existing values are not overwritten, even if they differ.
            results[key(row)] = {"account": row["account"], "nmID": int(row["nmID"]), "vendorCode": row["vendorCode"],
                "status": "ALREADY_FILLED" if first == second == CODE else "SKIPPED_NONEMPTY", "at": now(),
                "wb_15000001": first, "wb_15004139": second}
            save("results.json", results)
            continue
        save(f"before/{key(row)}.json", card)
        payload = pilot_request(card, CODE)[0]
        documents = card.get("documents")
        if documents is not None:
            if documents.get("items"):
                raise ValueError("Attached documents require an explicit preservation mapping; stop before update")
            payload["documents"] = {"items": [], "excludeDocuments": documents.get("excludeDocuments", False)}
        save(f"requests/{key(row)}.json", payload)
        ready.append((row, card, payload))
    if not ready:
        return
    account = ready[0][0]["account"]
    if any(row["account"] != account for row, _, _ in ready):
        raise ValueError("Batch must belong to one account")
    send(session, account, [payload for _, _, payload in ready])
    time.sleep(6)
    for row, card, _ in ready:
        verify(session, row, card, results)
    print(account, "batch verified", len(ready), "total results", len(results), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["preview", "pilot", "apply", "verify"], default="preview")
    args = parser.parse_args()
    rows = scope()
    for sub in ["before", "after", "requests"]:
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    result_path = OUT / "results.json"
    results = json.loads(result_path.read_text()) if result_path.exists() else {}
    with requests.Session() as session:
        if args.mode == "preview":
            dictionary = session.get("https://content-api.wildberries.ru/content/v2/directory/tnved",
                headers={"Authorization": _content_token("wb_acc1")}, params={"subjectID": 1820}, timeout=40)
            dictionary.raise_for_status(); data = dictionary.json(); save("dictionary_1820.json", data)
            if CODE not in {str(r["tnved"]) for r in data.get("data", [])}:
                raise ValueError("Owner's code is not in current WB dictionary")
            preview = {"at": now(), "total": len(rows), "accounts": dict(Counter(r["account"] for r in rows)),
                "subjectID": 1820, "code": CODE, "fields": list(FIELDS), "criterion": "Both fields empty on fresh read",
                "source": "Direct owner instruction, 05.10.2026; overrides TC code for this WB subject", "writes": 0}
            save("preview.json", preview); print(json.dumps(preview, ensure_ascii=False), flush=True)
            return
        if args.mode == "pilot":
            # First card from each account: normal and enabled-wholesale flows.
            for account in ("wb_acc1", "wb_acc2"):
                candidate = next(row for row in rows if row["account"] == account)
                apply_batch(session, [candidate], results)
            pilots = [results.get(key(next(row for row in rows if row["account"] == a))) for a in ("wb_acc1", "wb_acc2")]
            verified = all(r and r["status"] in ("VERIFIED", "ALREADY_FILLED") for r in pilots)
            save("pilot_gate.json", {"at": now(), "verified": verified, "pilots": pilots,
                                      "wholesale_enabled_verified": any(r and r.get("wholesale_enabled") and r["status"] == "VERIFIED" for r in pilots)})
            if not verified:
                raise RuntimeError("Pilot not confirmed")
            return
        if args.mode == "apply":
            gate = json.loads((OUT / "pilot_gate.json").read_text())
            if not gate.get("verified") or not gate.get("wholesale_enabled_verified"):
                raise ValueError("Normal and enabled-wholesale pilot verification required")
            for account in ("wb_acc1", "wb_acc2"):
                part = [row for row in rows if row["account"] == account]
                for i in range(0, len(part), 20):
                    apply_batch(session, part[i:i+20], results)
        else:
            for i, row in enumerate(rows, 1):
                before_path = OUT / "before" / f"{key(row)}.json"
                if before_path.exists():
                    verify(session, row, json.loads(before_path.read_text()), results, seconds=1)
                if i % 40 == 0:
                    print("Final verification", i, "/", len(rows), flush=True)
        summary = {"at": now(), "scope": len(rows), "statuses": dict(Counter(r["status"] for r in results.values())),
                   "accounts": {a: dict(Counter(r["status"] for r in results.values() if r["account"] == a)) for a in ("wb_acc1", "wb_acc2")},
                   "mode": args.mode, "code": CODE, "tc_writes": 0, "category_writes": 0}
        save("summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
