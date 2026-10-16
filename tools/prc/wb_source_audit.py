# поток: prc
"""Read-only WB name check for cards with saved novelty provenance. No apply mode.

The frozen 200-card mass plan is deliberately independent of this report.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core import db, ms_api
from prices.wb_source import for_product


def check(product):
    try:
        result = for_product(product)
    except ValueError as exc:
        return {"code": product.get("code"), "status": "HOLD", "reason": str(exc)}
    current = next((a.get("value") for a in product.get("attributes", [])
                    if a.get("name") == "Название WB"), "")
    return {"code": product.get("code"), "current": current, "expected": result["name"],
            "status": "HOLD" if not result["name"] else "OK" if current == result["name"] else "DIFF",
            "reason": result["reason"], "source_code": result["source_code"],
            "rules_version": result["rules_version"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--live", action="store_true", help="GET saved cards from MS instead of DB snapshot")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.code:
        # Also compare existing catalogue-backed cards through the same resolver.
        rows = db.query("""SELECT payload->>'code' card_code, ms_id, payload
                            FROM raw_moysklad_product WHERE payload->>'code' = ANY(%s)""",
                        (args.code,))
        found = {r["card_code"] for r in rows}
        missing = [code for code in args.code if code not in found]
        rows += db.query("""SELECT card_code, ms_id, NULL::jsonb payload FROM prc_wb_name_source
                            WHERE card_code = ANY(%s)""", (missing,)) if missing else []
        found = {r["card_code"] for r in rows}
        rows += [{"card_code":code, "ms_id":None} for code in args.code if code not in found]
    else:
        rows = db.query("""SELECT s.card_code, s.ms_id, r.payload FROM prc_wb_name_source s
                           LEFT JOIN raw_moysklad_product r ON r.ms_id = s.ms_id
                           ORDER BY s.card_code""")
    checks = []
    for row in rows:
        if not row["ms_id"]:
            checks.append({"code": row["card_code"], "status": "HOLD",
                           "reason": "карточка не найдена или её создание МС не подтверждено"})
            continue
        product = ms_api.get("/entity/product/" + str(row["ms_id"])) if args.live else row["payload"]
        checks.append(check(product) if product else
                      {"code": row["card_code"], "status": "HOLD", "reason": "нет снимка карточки"})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    print(json.dumps({"cards": len(rows), "statuses": dict(Counter(c["status"] for c in checks)),
                      "ms_writes": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
