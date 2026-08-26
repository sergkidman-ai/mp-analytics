# поток: prc — привести «Название WB» уже заведённых карточек к формуле (правило 41).
"""Диалог «➕ В МС» с 11.08 по 26.08.2026 писал в поле ПЕРЕСКАЗ названия поставщика
(`wb_name`), а не формулу (`wb_compose`): строка родилась 11.08, формула приехала 14.08,
правило «брать как у родни» отменено 16.08 — диалог забыли перевести. Здесь чиним хвост.

    ./venv/bin/python tools/prc/wb_name_repair.py --backup backups/wb_name_<дата>.json
    ./venv/bin/python tools/prc/wb_name_repair.py --backup ... --apply

Карточки берём ровно те, что завёл диалог: у строки «Новинок» заполнен `link` (в нём внешние
коды всех карточек группы). Значение считает `ms_import.wb_compose` — единственная формула
на проект. Совпало с формулой — карточку не трогаем.
"""
import sys, json, csv, time, argparse
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import db, ms_api
from prices import ms_import
from tools.prc.wb_fill import WB_ATTR, apply

ap = argparse.ArgumentParser()
ap.add_argument("--since", default="2026-08-11")
ap.add_argument("--backup", required=True)
ap.add_argument("--report", default="docs/reports/prc_wb_name_repair_2026-08-26.csv")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

codes = sorted({ec.strip() for r in db.query(
    "SELECT link FROM prc_novelty WHERE decision='exists' AND link IS NOT NULL"
    "   AND decided_at >= %s", (a.since,)) for ec in (r["link"] or "").split(";") if ec.strip()})

cards = {}
for i in range(0, len(codes), 40):
    chunk = codes[i:i + 40]
    got = ms_api.get("/entity/product",
                     {"filter": ";".join(f"externalCode={c}" for c in chunk), "limit": 100})
    for p in got.get("rows", []):
        cards[p.get("externalCode")] = p
    time.sleep(0.3)

def wb_of(p):
    return next(((x.get("value") or "").strip() for x in p.get("attributes") or []
                 if x.get("name") == WB_ATTR), "")

todo, same, blank = [], 0, []
for ec in codes:
    p = cards.get(ec)
    if not p:
        continue
    have = wb_of(p)
    want, note = ms_import.wb_compose(ec, p.get("name") or "")
    if not want:
        blank.append((ec, p.get("code"), note))
        continue
    if want == have:
        same += 1
        continue
    todo.append({"ms_id": p["id"], "code": p.get("code"), "external_code": ec,
                 "ms_name": p.get("name"), "was": have, "wb_name": want,
                 "note": note or "", "attributes": p.get("attributes") or []})

json.dump([{"ms_id": r["ms_id"], "code": r["code"], "external_code": r["external_code"],
            "Название WB": r["was"]} for r in todo],
          open(a.backup, "w"), ensure_ascii=False, indent=1)
with open(a.report, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh, delimiter=";")
    w.writerow(["внешний код", "код", "наименование МС", "было", "станет", "замечание"])
    for r in todo:
        w.writerow([r["external_code"], r["code"], r["ms_name"], r["was"], r["wb_name"], r["note"]])

print(f"карточек диалога: {len(cards)} | к правке: {len(todo)} | уже по формуле: {same} "
      f"| формула не собралась: {len(blank)}")
for r in todo:
    mark = "*" if r["note"] else " "
    print(f"{mark}{r['code'] or r['external_code']:>9} | {(r['was'] or '— пусто')[:44]:<44} → {r['wb_name'][:44]}")
for ec, code, note in blank:
    print(f"  {code or ec:>9} | НЕ ТРОГАЕМ: {note[:80]}")
print(f"\n* — собрано из названия поставщика, каталог ТК кода не знает. Бэкап: {a.backup}")
if not a.apply:
    print(f"[проба] в МойСклад не записано ничего; отчёт {a.report}")
else:
    apply(todo, dry=False)
