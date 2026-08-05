# поток: prc
# -*- coding: utf-8 -*-
"""Сверка нашего прогона с ручной загрузкой: позиции документов МС vs prc_price_row.

Запуск: PYTHONPATH=. ./venv/bin/python tools/prc_verify_vs_manual.py <supplier> <load_id>
"""
from collections import Counter
from decimal import Decimal
from core import ms_api
from core.db import query
from prices.loader import existing_docs
from prices.profiles import get_profile

import sys
supplier = sys.argv[1] if len(sys.argv) > 1 else "colortek"
load_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
prof = get_profile(supplier)
docs = existing_docs(prof)
manual = {}
for d in docs:
    for p in ms_api.iter_rows(f"/entity/enter/{d['id']}/positions"):
        aid = p["assortment"]["meta"]["href"].rstrip("/").split("/")[-1].split("?")[0]
        manual[aid] = (Decimal(str(p["quantity"])), int(p["price"]))

ours = {}
for r in query("select ms_id, qty, price_rub from prc_price_row where load_id=%s and status='loaded'", (load_id,)):
    ours[r["ms_id"]] = (Decimal(r["qty"]), int(Decimal(r["price_rub"]) * 100))

only_ms = set(manual) - set(ours)
only_our = set(ours) - set(manual)
diff_qty = [k for k in set(manual) & set(ours) if manual[k][0] != ours[k][0]]
diff_price = [k for k in set(manual) & set(ours) if manual[k][1] != ours[k][1]]
print(f"позиций: ручная {len(manual)}, наша {len(ours)}")
print(f"только в ручной: {len(only_ms)}; только у нас: {len(only_our)}")
print(f"расхождений количества: {len(diff_qty)}; цены: {len(diff_price)}")
for k in (diff_qty + diff_price)[:5]:
    print("  ", k, "МС", manual[k], "наше", ours[k])
