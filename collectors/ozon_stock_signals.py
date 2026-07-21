"""collectors/ozon_stock_signals.py — сигналы оборачиваемости Ozon FBO по SKU.

POST /v1/analytics/stocks (батчи по 100 sku) → days_without_sales / turnover_grade / excess /
ads / idc. Ответ приходит per-SKU × кластер — агрегируем per-SKU (max days_without_sales),
снимок на дату → ozon_stock_signals. Источник списка SKU — последний снимок ozon_fbo_stock
(то, что реально лежит на FBO прямо сейчас, включая товар, добавленный инвентаризацией).

Основной сигнал к вывозу — days_without_sales: точного per-SKU «платного хранения» Ozon API
не отдаёт (проверено), а excess_stock_count по картриджам всегда 0. Точный склад и количество
для заявки берём из ozon_fbo_stock; здесь только сигналы (join по account+sku в движке отбора).

Запуск:  ./venv/bin/python collectors/ozon_stock_signals.py [oz_acc1]
"""
import sys
import time
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                       # noqa: E402
from collectors.ozon import _headers      # noqa: E402

URL = "https://api-seller.ozon.ru/v1/analytics/stocks"
BATCH = 100


def fetch(account, skus):
    """{sku: {offer_id, name, dws, excess, ads, idc, grades}} — агрегат per-SKU по кластерам."""
    H = _headers(account)
    agg = {}
    for i in range(0, len(skus), BATCH):
        batch = skus[i:i + BATCH]
        for _ in range(6):
            r = requests.post(URL, headers=H, json={"skus": batch}, timeout=90)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "3")) + 2)
                continue
            r.raise_for_status()
            break
        for it in r.json().get("items", []):
            s = str(it["sku"])
            a = agg.setdefault(s, {"offer_id": it.get("offer_id"), "name": it.get("name"),
                                   "dws": 0, "excess": 0, "ads": 0.0, "idc": None, "grades": set()})
            a["dws"] = max(a["dws"], it.get("days_without_sales") or 0)
            a["excess"] += it.get("excess_stock_count") or 0
            a["ads"] = max(a["ads"], it.get("ads") or 0.0)
            if it.get("idc") is not None:
                a["idc"] = it["idc"] if a["idc"] is None else max(a["idc"], it["idc"])
            if it.get("turnover_grade"):
                a["grades"].add(it["turnover_grade"])
        time.sleep(0.5)
    return agg


def main(account="oz_acc1"):
    print(f"Ozon сигналы {account}", flush=True)
    cap = datetime.date.today().isoformat()
    skus = [str(r["sku"]) for r in db.query(
        "SELECT DISTINCT sku FROM ozon_fbo_stock WHERE account=%s "
        "AND captured_at=(SELECT max(captured_at) FROM ozon_fbo_stock WHERE account=%s) "
        "AND sku IS NOT NULL", (account, account))]
    if not skus:
        print("  нет SKU в снимке стока — сначала собери ozon_fbo_stock", flush=True)
        return
    agg = fetch(account, skus)
    recs = [{"account": account, "sku": s, "offer_id": a["offer_id"], "name": a["name"],
             "days_without_sales": a["dws"],
             "turnover_grade": ",".join(sorted(a["grades"])) or None,
             "excess_stock_count": a["excess"], "ads": a["ads"], "idc": a["idc"],
             "captured_at": cap}
            for s, a in agg.items()]
    n = db.upsert("ozon_stock_signals", recs,
                  conflict_cols=["account", "sku", "captured_at"],
                  update_cols=["offer_id", "name", "days_without_sales", "turnover_grade",
                               "excess_stock_count", "ads", "idc"])
    stale = sum(1 for r in recs if r["days_without_sales"] >= 90)
    print(f"Записано SKU: {n} | без продаж ≥90д: {stale}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")
