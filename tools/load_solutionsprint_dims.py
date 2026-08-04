# -*- coding: utf-8 -*-
# поток: gab
"""Влить harvest cartridge.ru (B2B Солюшнс принт) в supplier_dims как supplier='solutionsprint'.

Источник — dims.jsonl (crawl_dims_ms.py): {code,title,analog,dims_mm[L,W,H мм],vol_l,weight_kg}.

РЕШЕНИЯ ПОСЛЕ CODEX-РЕВЬЮ (см. docs/GABARITY_SOLUTIONSPRINT_HANDOFF.md):
- title-ONLY: поле analog НЕ используем (парсер брал его из карусели «сопутствующие» → налипал на чужие
  товары; для матча берём OEM из title, он надёжен). analog кладём в отдельную колонку title как справку? нет —
  вообще не пишем, чтобы matcher не подхватил ложный код.
- ОБЪЁМ из ДхШхВ, а не из поля «Объём» сайта (там встречается мусор, напр. смола vol=1000 л). Поле «Объём»
  берём только когда ДхШхВ нет, и то с валидацией диапазона.
- Валидация сторон (>0, ≤200 см) и объёма (0.05..60 л).
- Дедуп по коду: оставляем наиболее полную строку (с ДхШхВ важнее, чем только объём).
dry-run по умолчанию; --execute пишет.
"""
import json, sys, pathlib
sys.path.insert(0, "/opt/mp-analytics")
from core import db

SRC = pathlib.Path("/opt/mp-analytics/incoming/gab/solutionsprint_dims.jsonl")
SUPPLIER = "solutionsprint"
EXECUTE = "--execute" in sys.argv

def good_side(x):
    try: return 0 < float(x) <= 200
    except (TypeError, ValueError): return False

def good_vol(v):
    try: return 0.05 <= float(v) <= 60
    except (TypeError, ValueError): return False

raw = [json.loads(l) for l in open(SRC, encoding="utf-8") if l.strip()]

# дедуп по коду: приоритет строке с валидными ДхШхВ
best = {}
def completeness(r):
    d = r.get("dims_mm")
    return (1 if (d and len(d) == 3 and all(good_side(x/10.0) for x in d)) else 0,
            1 if good_vol((r.get("vol_l") or 0)) else 0)
for r in raw:
    if r.get("err") or not r.get("code"): continue
    c = str(r["code"])
    if c not in best or completeness(r) > completeness(best[c]):
        best[c] = r

rows = []
for c, r in best.items():
    d = r.get("dims_mm")
    has_dim = bool(d and len(d) == 3 and all(good_side(x/10.0) for x in d))
    L = W = H = None
    if has_dim:
        L, W, H = round(d[0]/10.0, 2), round(d[1]/10.0, 2), round(d[2]/10.0, 2)
        v = round(L*W*H/1000.0, 3)                 # объём ИЗ КОРОБА (для тарифа)
    else:
        v = r.get("vol_l")                          # поля «Объём» — только когда нет ДхШхВ
        if not good_vol(v): continue                # мусор/нет объёма → пропуск
        v = round(float(v), 3)
    if not good_vol(v): continue
    rows.append({
        "supplier": SUPPLIER, "article": c, "barcode": None,
        "length_cm": L, "width_cm": W, "height_cm": H,
        "weight_kg": (round(float(r["weight_kg"]), 3) if r.get("weight_kg") else None),
        "volume_l": v, "title": (r.get("title") or "")[:400],   # title-ONLY, без analog
        "src_file": "cartridge.ru B2B scrape 2026-07-22",
    })

n_dim = sum(1 for x in rows if x["length_cm"] is not None)
n_w = sum(1 for x in rows if x["weight_kg"] is not None)
print(f"harvest строк {len(raw)}, уник кодов {len(best)} → к загрузке {len(rows)} (с ДхШхВ {n_dim}, с весом {n_w})")
for x in rows[:5]:
    print(f"  {x['article']:8} vol={x['volume_l']:6} dim={x['length_cm']}x{x['width_cm']}x{x['height_cm']} w={x['weight_kg']} | {x['title'][:52]}")

if not rows:
    print("НЕТ строк к загрузке — прекращаю (пустой INSERT недопустим)."); sys.exit(1)
if not EXECUTE:
    print("\n[dry-run] запись не выполнена. Повтори с --execute."); sys.exit(0)

with db.get_conn() as conn, conn.cursor() as cur:
    cur.execute("DELETE FROM supplier_dims WHERE supplier=%s", (SUPPLIER,))
    deleted = cur.rowcount
    cols = ["supplier","article","barcode","length_cm","width_cm","height_cm","weight_kg","volume_l","title","src_file"]
    vals = [tuple(x[c] for c in cols) for x in rows]
    ph = "(" + ",".join(["%s"]*len(cols)) + ")"
    args = b",".join(cur.mogrify(ph, v) for v in vals)
    cur.execute(b"INSERT INTO supplier_dims (" + b",".join(c.encode() for c in cols) + b") VALUES " + args)
    print(f"\nудалено прежних solutionsprint: {deleted}; вставлено: {len(rows)}")
