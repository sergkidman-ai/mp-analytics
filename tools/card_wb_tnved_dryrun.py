#!/usr/bin/env python3
# поток: card
"""Offline TN VED plan from preserved CSVs and read-only WB responses.

No credentials, DB, network, --apply option or marketplace mutation code.
"""
import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FIELDS = (15000001, 15004139)
SUBJECTS = {"Картриджи для принтеров": 1820, "Чернила для принтера": 1907}
DOCS = "https://dev.wildberries.ru/en/openapi/work-with-products"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def code_of(value):
    match = re.fullmatch(r"(\d{10})(?:\s*[-–].*)?", value.strip())
    return match[1] if match else ""


def write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def pilot_request(card, code):
    """Preserve editable values; change only the two previously empty fields."""
    if not re.fullmatch(r"\d{10}", code):
        raise ValueError("Missing or invalid source code")
    for field in FIELDS:
        if any(c.get("value") for c in card.get("characteristics", []) if c["id"] == field):
            raise ValueError("Pilot no longer has both fields empty; do not overwrite")
    keys = ("nmID", "vendorCode", "brand", "title", "description", "kizMarked")
    payload = {key: deepcopy(card[key]) for key in keys if key in card}
    payload["dimensions"] = {key: deepcopy(value) for key, value in card["dimensions"].items()
                             if key in ("length", "width", "height", "weightBrutto")}
    payload["sizes"] = [{key: deepcopy(value) for key, value in size.items()
                         if key in ("chrtID", "techSize", "wbSize", "skus")} for size in card["sizes"]]
    payload["characteristics"] = [{"id": c["id"], "value": deepcopy(c["value"])}
                                  for c in card["characteristics"] if c["id"] not in FIELDS]
    payload["characteristics"] += [{"id": field, "value": [code]} for field in FIELDS]
    # Preserve wholesale baseline in the draft. Its update-schema support must
    # be checked separately before execution; this tool can never send it.
    if "wholesale" in card:
        payload["wholesale"] = deepcopy(card["wholesale"])
    return [payload]


def build(source, out):
    dictionaries = {sid: read_json(out / f"wb_tnved_{sid}.json") for sid in SUBJECTS.values()}
    allowed = {sid: {str(row["tnved"]) for row in data["response"]["data"]}
               for sid, data in dictionaries.items()}
    schema = {sid: read_json(out / f"wb_charcs_{sid}.json") for sid in SUBJECTS.values()}
    for sid, data in schema.items():
        for field in FIELDS:
            found = [c for c in data["response"]["data"] if c["charcID"] == field]
            if len(found) != 1 or found[0]["charcType"] != 1:
                raise ValueError(f"Unverified characteristic schema: {sid}/{field}")
    rows, hashes, seen = [], {}, set()
    for account in ("wb_acc1", "wb_acc2"):
        path = source / f"{account}_missing_both.csv"
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open(encoding="utf-8-sig", newline="") as handle:
            inputs = list(csv.DictReader(handle, delimiter=";"))
        for row in inputs:
            nm = int(row["Артикул ВБ"])
            if (account, nm) in seen:
                raise ValueError("Duplicate account/nmID in input")
            seen.add((account, nm))
            if any(row[key].strip() for key in ("ТНВЭД 15000001", "Код ТН ВЭД 15004139")):
                raise ValueError("Source CSV violates both-empty criterion")
            sid = SUBJECTS.get(row["Предмет ВБ"])
            raw_code = row["ТН ВЭД в ТК"]
            code = code_of(raw_code)
            if not raw_code.strip():
                status, reason = "NO_SOURCE", "Нет сохранённого кода ТК; значение не предлагается"
            elif not code:
                status, reason = "REVIEW", "Некорректный формат сохранённого кода ТК"
            elif code not in allowed.get(sid, set()):
                status, reason = "REVIEW", "Код ТК отсутствует в справочнике ТНВЭД этого предмета WB"
            else:
                status, reason = "DICTIONARY_MATCH", "Код ТК допустим для предмета WB; физический товар не проверен"
            rows.append({**row, "account": account, "subjectID": sid, "proposed_code": code,
                         "dry_run_status": status, "reason": reason})
    columns = list(rows[0])
    write_csv(out / "plan.csv", rows, columns)
    for name, status in (("candidates.csv", "DICTIONARY_MATCH"), ("review.csv", "REVIEW"),
                         ("no_source.csv", "NO_SOURCE")):
        write_csv(out / name, [r for r in rows if r["dry_run_status"] == status], columns)
    groups = Counter((r["subjectID"], r["Предмет ВБ"], r["proposed_code"], r["dry_run_status"])
                     for r in rows)
    write_csv(out / "subject_code_map.csv", [{"subjectID": sid, "subject": subject, "code": code,
               "status": status, "count": count} for (sid, subject, code, status), count in sorted(groups.items())],
              ["subjectID", "subject", "code", "status", "count"])
    live = read_json(out / "0022X2_live.json")
    card = live["card"]
    matches = [r for r in rows if r["account"] == live["account"] and int(r["Артикул ВБ"]) == card["nmID"]]
    if len(matches) != 1:
        raise ValueError("Pilot not uniquely present in CSV")
    row = matches[0]
    if (row["Внешний код карточки"] != card["vendorCode"] or row["subjectID"] != card["subjectID"]
            or row["dry_run_status"] != "DICTIONARY_MATCH"):
        raise ValueError("Pilot source identity or code validation failed")
    request = pilot_request(card, row["proposed_code"])
    write_json(out / "0022X2_request_draft.json", request)
    original = [{"id": c["id"], "value": c["value"]} for c in card["characteristics"] if c["id"] not in FIELDS]
    assert request[0]["characteristics"][:-2] == original
    assert request[0]["sizes"] == [{k: v for k, v in s.items() if k in ("chrtID", "techSize", "wbSize", "skus")} for s in card["sizes"]]
    audit = {"sent": False, "endpoint": "POST https://content-api.wildberries.ru/content/v2/cards/update",
             "account": live["account"], "nmID": card["nmID"], "source_read_at": live["read_at"],
             "added_characteristics": [{"id": field, "value": [row["proposed_code"]]} for field in FIELDS],
             "existing_characteristic_values_preserved": True, "other_editable_values_preserved": True,
             "wholesale_schema_pending": "wholesale" in card,
             "execution_authorized": False,
             "execution_requirements": ["Прямая команда пользователя", "Проверка физического товара и источника кода",
                "Свежая перечитка карточки: оба поля пусты, идентичность и прочие поля совпадают",
                "Подтверждение актуального контракта wholesale перед отправкой полного запроса",
                "Перечитка после отправки: коды появились, остальные значения сохранились"]}
    write_json(out / "0022X2_audit.json", audit)
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_csv_sha256": hashes,
               "total": len(rows), "statuses": dict(Counter(r["dry_run_status"] for r in rows)),
               "accounts": {a: dict(Counter(r["dry_run_status"] for r in rows if r["account"] == a))
                            for a in ("wb_acc1", "wb_acc2")},
               "dictionary_read_at": {sid: data["read_at"] for sid, data in dictionaries.items()},
               "bulk_cards_refreshed": False, "tk_codes_refreshed": False, "marketplace_writes": 0,
               "db_access": False, "pilot": audit, "api_documentation": DOCS}
    write_json(out / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "docs/reports/wb_tnved_empty_fields_2026-10-04")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/reports/wb_tnved_dry_run_2026-10-05")
    args = parser.parse_args()
    summary = build(args.source, args.out)
    print(json.dumps({key: summary[key] for key in ("total", "statuses", "marketplace_writes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
