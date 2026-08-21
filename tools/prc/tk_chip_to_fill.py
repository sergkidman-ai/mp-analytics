# поток: prc
"""Список внешних кодов, где чип в ТК пуст, а ≥2 поставщика в названии пишут одно и то же."""
import sys, csv, collections, pathlib
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from prices import features
from tools.prc.tc_fields import write_xlsx  # noqa: F401  (берём только openpyxl-обвязку ниже)
RU = features.CHIP_NAMES

sup = collections.defaultdict(list)
for r in db.query("""select external_code ec, supplier, name, stock from supplier_stock
                     where captured_at=(select max(captured_at) from supplier_stock)
                       and store='Удаленный склад' and stock>0"""):
    t = features.chip(r["name"])
    if t: sup[(r["ec"] or "").strip()].append((r["supplier"], r["name"], t, r["stock"]))

tk = {r["external_code"]: r for r in db.query("""select external_code, title, additional_title,
        united_title, color, resource, chip, consumable_type from prc_tc_model where gone_at is null""")}

src = [r for r in csv.DictReader(open("/opt/mp-analytics/docs/reports/"
        "prc_tc_fields_chip_conflicts_2026-08-20.csv", encoding="utf-8-sig"), delimiter=";")
       if r["тип расхождения"] == "в ТК нет данных"]
cards = collections.defaultdict(list)
for r in src:
    cards[(r["внешний код"] or "").strip()].append(r["код"])

stat = collections.Counter(); rows = []
for ec in sorted({(r["внешний код"] or "").strip() for r in src}):
    items = sup.get(ec, [])
    toks = collections.Counter(x[2] for x in items)
    if not toks: stat["не с чем сравнить"] += 1; continue
    if len(toks) > 1: stat["разнобой у поставщиков"] += 1; continue
    n = sum(toks.values())
    if n < 2: stat["источник один"] += 1; continue
    stat["≥2 поставщика согласны"] += 1
    t = tk.get(ec) or {}
    rows.append({
        "внешний код": ec,
        "модель ТК": t.get("title") or "",
        "доп. название": t.get("additional_title") or "",
        "серия": t.get("united_title") or "",
        "цвет": t.get("color") or "",
        "ресурс": t.get("resource") or "",
        "чип в ТК сейчас": RU[t.get("chip") or None] if t else "модели нет в ТК",
        "поставить в ТК": RU[next(iter(toks))],
        "согласны поставщиков": n,
        "как пишут поставщики": " | ".join(sorted({x[1][:48] for x in items})[:3]),
        "остаток на Удаленном": sum(x[3] for x in items),
        "наши карточки МС": ", ".join(sorted(set(cards[ec]))[:8]),
    })
for k, v in stat.most_common(): print(f"  {k:30} {v}")
print("строк в списке:", len(rows))

from openpyxl import Workbook
from openpyxl.styles import Font
cols = list(rows[0].keys())
wb = Workbook(); ws = wb.active; ws.title = "чип — заполнить в ТК"
ws.append(cols)
for c in ws[1]: c.font = Font(bold=True)
for r in rows: ws.append([r[c] for c in cols])
for i, w in enumerate((12, 18, 16, 22, 8, 10, 18, 22, 12, 56, 20, 30), start=1):
    ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
p = pathlib.Path("/opt/mp-analytics/docs/reports/prc_tk_chip_to_fill_2026-08-21.xlsx")
wb.save(p)
with p.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as fh:
    w = csv.DictWriter(fh, fieldnames=cols, delimiter=";"); w.writeheader(); w.writerows(rows)
print("файл:", p)
