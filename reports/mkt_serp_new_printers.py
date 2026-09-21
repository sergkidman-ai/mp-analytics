#!/usr/bin/env python3
# поток: mkt
"""reports/mkt_serp_new_printers.py — этап 4 промта: модели принтеров у конкурентов, которых
нет в наших названиях (docs/prompts/mkt_wb_scraper_start.md).

ЭТО ЭВРИСТИКА, НЕ ФАКТ. Регексом достаём алфанумерный токен после известного бренда из названия
строки выдачи (wb_search_snapshot.name, только item_kind cartridge/drum/head — этап 2), убираем
токены, совпадающие с цифрами самого поискового запроса (это код картриджа, который мы искали,
а не модель принтера — «Canon PG-445 CL-446» по запросу «...445 и 446» даёт «445»/«446» как
ложных кандидатов без этого фильтра) и сверяем нормализованный остаток с `compat_index.model_core`
(наши уже известные принтеры, 7500+ моделей). Результат — статус CANDIDATE, как в gab-харвестере:
нужна проверка человеком, не заводить в карточки/цены автоматом.

    ./venv/bin/python reports/mkt_serp_new_printers.py [--min-count 2]
    → docs/reports/mkt_new_printer_candidates_<ГГГГММДД>.csv
"""
import argparse
import csv
import datetime
import pathlib
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

RELEVANT_KINDS = ("cartridge", "drum", "head")
BRANDS = (r"canon|hp|epson|brother|samsung|xerox|kyocera|ricoh|pantum|konica|panasonic|"
          r"oki|lexmark|sharp|toshiba|dell|xiaomi|deli|pixma")
MODEL_TOK = r"[A-Za-zА-Яа-я]{0,4}-?\d{2,5}[A-Za-zА-Яа-я]{0,5}"
PAT = re.compile(BRANDS + r"\s+(" + MODEL_TOK + r"(?:\s+" + MODEL_TOK + r"){0,2})", re.I)
DIGIT_RUN = re.compile(r"\d{2,5}")


def norm(tok):
    return re.sub(r"[\s\-]", "", tok).lower()


def candidates(min_count):
    rows = db.query(
        "SELECT s.query, s.name FROM wb_search_snapshot s "
        "JOIN wb_search_match m ON m.snapshot_id = s.id "
        "WHERE m.item_kind = ANY(%s)", (list(RELEVANT_KINDS),))
    known = {norm(r["model_core"]) for r in db.query(
        "SELECT DISTINCT model_core FROM compat_index WHERE platform = 'wb'")}

    found = {}   # norm_token -> {"count": n, "examples": set(names)}
    for r in rows:
        query_codes = {norm(d) for d in DIGIT_RUN.findall(r["query"] or "")}
        for m in PAT.findall(r["name"] or ""):
            for tok in m.split():
                n = norm(tok)
                if len(n) < 3 or not any(c.isdigit() for c in n):
                    continue
                if any(qc in n or n in qc for qc in query_codes):
                    continue           # это код картриджа из самого запроса, не принтер
                if n in known:
                    continue           # у нас уже есть такой принтер
                d = found.setdefault(n, {"count": 0, "examples": set()})
                d["count"] += 1
                d["examples"].add((r["name"] or "")[:80])

    out = [{"model_candidate": k, "count": v["count"],
            "examples": " | ".join(list(v["examples"])[:2]), "status": "CANDIDATE"}
           for k, v in found.items() if v["count"] >= min_count]
    out.sort(key=lambda x: -x["count"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-count", type=int, default=2,
                    help="минимум упоминаний у РАЗНЫХ строк выдачи, чтобы не тащить опечатки")
    a = ap.parse_args()

    rows = candidates(a.min_count)
    out_path = (BASE_DIR / "docs" / "reports" /
                f"mkt_new_printer_candidates_{datetime.date.today():%Y%m%d}.csv")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["model_candidate", "count", "examples", "status"],
                           delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"кандидатов: {len(rows)} → {out_path}")
    for r in rows[:15]:
        print(f"  {r['count']:>3}  {r['model_candidate']}  ({r['examples'][:60]})")


if __name__ == "__main__":
    main()
