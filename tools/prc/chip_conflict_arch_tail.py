# поток: prc
"""Хвост чип-противоречий (карточки-тёзки под одним кодом) — в архив с обнулением.

Разводим по `id`: код в МС не уникален, под одним кодом до 9 одинаковых карточек. Гейт —
нуль на НАШИХ складах («Удаленный»/«Транзит» не наши). Удаление не применяется (решение 21.08).
"""
import sys, csv, json, time, pathlib, collections, argparse
sys.path.insert(0, "/opt/mp-analytics")
from core import ms_api
from tools.prc import tc_fields as T

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true"); ap.add_argument("--pause", type=float, default=2.0)
args = ap.parse_args()

VIRTUAL = {"Удаленный склад", "Транзит"}
stores = {s["id"]: s["name"] for s in ms_api.get("/entity/store", {"limit": 100}).get("rows", [])}
stock = collections.defaultdict(dict)
raw = ms_api.get("/report/stock/bystore/current", {})
for r in (raw if isinstance(raw, list) else raw.get("rows", [])):
    if r.get("stock"):
        stock[r["assortmentId"]][stores.get(r.get("storeId"), "?")] = r["stock"]

handled = {f.stem.split("_", 1)[1] for d in ("prc_chip_conflict_drop_2026-08-20",
                                             "prc_chip_conflict_arch_2026-08-21")
           for f in pathlib.Path("/opt/mp-analytics/backups", d).glob("*.json")}
src = [r for r in csv.DictReader(open("/opt/mp-analytics/docs/reports/"
        "prc_tc_fields_chip_conflicts_2026-08-20.csv", encoding="utf-8-sig"), delimiter=";")
       if r["тип расхождения"] == "противоречие"]
tc_all = T.catalog()

targets = {}
for r in src:
    code = (r["код"] or "").strip()
    for c in ms_api.get("/entity/product", {"limit": 100, "filter": f"code={code}"}).get("rows", []):
        if c["id"] in handled or c["id"] in targets or c.get("archived"):
            continue
        if (c.get("name") or "").strip() != (r["наименование МС"] or "").strip():
            continue
        tc = tc_all.get((c.get("externalCode") or "").strip())
        if not tc or (T.plan_card(c, tc)[1].get("Чип") or ("", "", ""))[2] != "противоречие":
            continue
        targets[c["id"]] = c

bak = pathlib.Path("/opt/mp-analytics/backups/prc_chip_conflict_arch_tail_2026-08-21")
bak.mkdir(parents=True, exist_ok=True)
res = collections.Counter(); log = []
for c in targets.values():
    code = (c.get("code") or "").strip()
    st = stock.get(c["id"], {})
    ours = {k: v for k, v in st.items() if k not in VIRTUAL}
    if ours:
        res["держим: остаток на наших складах"] += 1
        log.append((code, c.get("externalCode"), c.get("name"),
                    "наш остаток: " + ", ".join(f"{k} {v:g}" for k, v in ours.items()))); continue
    where = ("виртуальный: " + ", ".join(f"{k} {v:g}" for k, v in st.items())) if st else "нуль везде"
    if not args.apply:
        res["к архивации"] += 1; log.append((code, c.get("externalCode"), c.get("name"),
                                             f"[проба] {where}")); continue
    (bak / f'{code or "нет"}_{c["id"]}.json').write_text(json.dumps(c, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    try:
        ms_api.put(f'/entity/product/{c["id"]}',
                   {"meta": c["meta"], "archived": True, "article": "", "code": "",
                    "externalCode": "", "barcodes": []})
        res["в архив с обнулением"] += 1
        log.append((code, c.get("externalCode"), c.get("name"), f"архив+обнуление ({where})"))
    except Exception as exc:
        res["ошибка"] += 1
        log.append((code, c.get("externalCode"), c.get("name"), f"ошибка: {type(exc).__name__}"))
    time.sleep(args.pause)

out = pathlib.Path("/opt/mp-analytics/docs/reports/prc_chip_conflict_arch_tail_2026-08-21.csv")
with out.open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh, delimiter=";")
    w.writerow(["код", "внешний код", "наименование МС", "что сделано"]); w.writerows(log)
print("карточек-целей:", len(targets))
for k, n in res.most_common(): print(f"  {k:36} {n}")
print("файл:", out)
