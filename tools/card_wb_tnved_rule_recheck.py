#!/usr/bin/env python3
# поток: card
"""Read-only recheck of the 209 TN VED exceptions against the owner's category rule."""
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from collectors.wb import CARDS_URL, _content_token
from core.db import query
from tools.card_wb_tnved_dryrun import code_of

SOURCE = ROOT / "docs/reports/wb_tnved_dry_run_2026-10-05"
OUT = ROOT / "docs/reports/wb_tnved_category_rule_recheck_2026-10-05"
SUBJECTS = {1820: "Картриджи для принтеров", 1907: "Чернила для принтера"}
INK_TYPES = {"чернила", "комплект чернил", "набор чернил"}


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def char(card, ident):
    value = next((c.get("value") for c in card.get("characteristics", []) if c["id"] == ident), None)
    return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value) if value is not None else ""


def fetch(session, account, nm):
    body = {"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm), "withPhoto": -1}}}
    for attempt in range(5):
        response = session.post(CARDS_URL, headers={"Authorization": _content_token(account)}, json=body, timeout=40)
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(min(30, max(2, int(response.headers.get("Retry-After", "3")))))
            continue
        response.raise_for_status()
        data = response.json()
        if data.get("error") or not isinstance(data.get("cards"), list):
            raise ValueError("Invalid WB cards response")
        found = [c for c in data["cards"] if c.get("nmID") == nm]
        if len(found) > 1:
            raise ValueError("Duplicate live target")
        time.sleep(.7)
        return found[0] if found else None
    raise RuntimeError("WB read retries exhausted")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with (SOURCE / "review.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    if len(rows) != 209 or len({(r["account"], r["Артикул ВБ"]) for r in rows}) != 209:
        raise ValueError("Unexpected source scope")
    models = query("SELECT external_code,consumable_type,ved_code,last_seen,gone_at FROM prc_tc_model WHERE external_code=ANY(%s)",
                   (sorted({r["Код основания в ТК"] for r in rows}),))
    save("tc_models_snapshot.json", models)
    tc = {r["external_code"]: r for r in models}
    allowed = {sid: {str(v["tnved"]) for v in json.loads((SOURCE / f"wb_tnved_{sid}.json").read_text())["response"]["data"]}
               for sid in SUBJECTS}
    results, counts = [], Counter()
    started = datetime.now(timezone.utc).isoformat()
    with requests.Session() as session, (OUT / "wb_live_observations.jsonl").open("w", encoding="utf-8") as raw:
        for i, row in enumerate(rows, 1):
            account, nm = row["account"], int(row["Артикул ВБ"])
            model = tc.get(row["Код основания в ТК"])
            kind = (model["consumable_type"] or "").strip() if model else ""
            expected = 1907 if kind.lower() in INK_TYPES else 1820 if kind else None
            card = fetch(session, account, nm)
            observed = datetime.now(timezone.utc).isoformat()
            raw.write(json.dumps({"read_at": observed, "account": account, "nmID": nm, "card": card}, ensure_ascii=False) + "\n")
            raw.flush()
            current = card.get("subjectID") if card else None
            if not model or not kind or model["gone_at"] is not None:
                verdict = "TC_SOURCE_UNRESOLVED"
            elif not card:
                verdict = "NOT_IN_ACTIVE_CATALOG"
            elif card.get("vendorCode") != row["Внешний код карточки"]:
                verdict = "IDENTITY_CHANGED"
            elif current == expected:
                verdict = "CATEGORY_CORRECT"
            else:
                verdict = "CATEGORY_MISMATCH"
            current_code = (model["ved_code"] or "").strip() if model else ""
            digits = code_of(current_code)
            field1 = char(card, 15000001) if card else ""
            field2 = char(card, 15004139) if card else ""
            results.append({"account": account, "vendorCode": row["Внешний код карточки"], "nmID": nm,
                "tc_base": row["Код основания в ТК"], "tc_type": kind, "tc_last_seen": str(model["last_seen"]) if model else "",
                "title": card.get("title", "") if card else row["Название"], "actual_subjectID": current,
                "actual_subject": SUBJECTS.get(current, str(current)), "expected_subjectID": expected,
                "expected_subject": SUBJECTS.get(expected, "UNKNOWN"), "category_verdict": verdict,
                "saved_tc_code": row["proposed_code"], "current_tc_code": digits,
                "code_allowed_actual_subject": digits in allowed.get(current, set()),
                "code_allowed_expected_subject": digits in allowed.get(expected, set()),
                "wb_15000001": field1, "wb_15004139": field2, "still_missing_both": not field1 and not field2,
                "wb_read_at": observed})
            counts[verdict] += 1
            if i % 40 == 0 or i == len(rows):
                print("WB live recheck", i, "/", len(rows), dict(counts), flush=True)
    columns = list(results[0])
    for name, selected in [("all_209.csv", results), ("category_mismatches.csv", [r for r in results if r["category_verdict"] == "CATEGORY_MISMATCH"]),
                           ("correct_category_code_conflicts.csv", [r for r in results if r["category_verdict"] == "CATEGORY_CORRECT" and not r["code_allowed_actual_subject"]])]:
        with (OUT / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
            writer.writeheader(); writer.writerows(selected)
    summary = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(), "total": len(results),
        "category_verdicts": dict(counts), "tc_type_counts": dict(Counter(r["tc_type"] for r in results)),
        "still_missing_both": sum(r["still_missing_both"] for r in results),
        "tc_code_changed_from_saved": sum(r["current_tc_code"] != r["saved_tc_code"] for r in results),
        "rule": "Чернила и комплекты/наборы чернил → 1907; все остальные непустые типы ТК → 1820",
        "rule_source": "Direct instruction from user, 05.10.2026", "marketplace_writes": 0, "db_writes": 0}
    save("summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
