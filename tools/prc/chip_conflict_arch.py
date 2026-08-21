# поток: prc
"""Чип-противоречия: карточки без нашего остатка — в архив с обнулением полей. БЕЗ удаления.

Решение Сергея 21.08.2026: не удаляем ничего, даже если МС позволяет; и карточки с нулём
на всех складах, и карточки с остатком только на «Удаленном» (виртуальный склад поставщика)
уходят в архив с очисткой идентификаторов — чтобы код/артикул освободились под новую верную
карточку. Наш физический остаток (Звездный, Дисквер, Озон, ФБО, ...) = карточку не трогаем.
Темп щадящий: в МС идут рабочие процессы.
"""
import sys, csv, json, time, pathlib, collections, argparse
sys.path.insert(0, "/opt/mp-analytics")
from core import ms_api

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--pause", type=float, default=1.5)
args = ap.parse_args()

VIRTUAL = {"Удаленный склад", "Транзит"}
stores = {s["id"]: s["name"] for s in ms_api.get("/entity/store", {"limit": 100}).get("rows", [])}
stock = collections.defaultdict(dict)
raw = ms_api.get("/report/stock/bystore/current", {})
for r in (raw if isinstance(raw, list) else raw.get("rows", [])):
    if r.get("stock"):
        stock[r["assortmentId"]][stores.get(r.get("storeId"), "?")] = r["stock"]

src = [r for r in csv.DictReader(open("/opt/mp-analytics/docs/reports/"
        "prc_tc_fields_chip_conflicts_2026-08-20.csv", encoding="utf-8-sig"), delimiter=";")
       if r["тип расхождения"] == "противоречие"]

bak = pathlib.Path("/opt/mp-analytics/backups/prc_chip_conflict_arch_2026-08-21")
bak.mkdir(parents=True, exist_ok=True)
res = collections.Counter(); log = []

def live_card(code, name):
    """Код в МС не уникален — разводим по наименованию; неоднозначность = не трогаем."""
    try:
        hits = ms_api.get("/entity/product", {"limit": 100, "filter": f"code={code}"}).get("rows", [])
    except Exception:
        return None
    same = [c for c in hits if (c.get("name") or "").strip() == (name or "").strip()]
    return same[0] if len(same) == 1 else (hits[0] if len(hits) == 1 else None)

for r in src:
    code = (r["код"] or "").strip()
    card = live_card(code, r["наименование МС"])
    if not card:
        res["не найдена/неоднозначно — на вечерний обход"] += 1
        log.append((code, r["внешний код"], "не найдена — вечерний живой обход")); continue
    if card.get("archived"):
        res["уже в архиве"] += 1; log.append((code, r["внешний код"], "уже в архиве")); continue
    st = stock.get(card["id"], {})
    ours = {k: v for k, v in st.items() if k not in VIRTUAL}
    if ours:
        res["держим: остаток на наших складах"] += 1
        log.append((code, r["внешний код"],
                    "остаток на наших складах: " + ", ".join(f"{k} {v:g}" for k, v in ours.items())))
        continue
    where = ("остаток только виртуальный: " + ", ".join(f"{k} {v:g}" for k, v in st.items())
             if st else "нуль на всех складах")
    if not args.apply:
        res["к архивации"] += 1; log.append((code, r["внешний код"], f"[проба] {where}")); continue
    (bak / f'{code or "нет"}_{card["id"]}.json').write_text(
        json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        ms_api.put(f'/entity/product/{card["id"]}',
                   {"meta": card["meta"], "archived": True, "article": "", "code": "",
                    "externalCode": "", "barcodes": []})
        res["в архив с обнулением"] += 1
        log.append((code, r["внешний код"], f"архив+обнуление ({where})"))
    except Exception as exc:
        res["ошибка"] += 1
        log.append((code, r["внешний код"], f"ошибка: {type(exc).__name__}: {str(exc)[:120]}"))
    time.sleep(args.pause)

out = pathlib.Path("/opt/mp-analytics/docs/reports/prc_chip_conflict_arch_2026-08-21.csv")
with out.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh, delimiter=";")
    w.writerow(["код", "внешний код", "что сделано"]); w.writerows(log)
print(f"строк «противоречие»: {len(src)}")
for k, n in res.most_common():
    print(f"  {k:44} {n}")
print("файл:", out)
