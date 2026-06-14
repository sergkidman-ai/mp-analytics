"""collectors/wb.py — Этап 2. WB финотчёт → raw_wb_report → sales.

reportDetailByPeriod (категория «Статистика», после 15.07.2026 → «Финансы»).
- Грузим ВСЕ строки отчёта в raw_wb_report (UPSERT по account+rrd_id) — полное сырьё.
- Нормализуем агрегатом по nm_id за период → sales (деньги: выручка, комиссия,
  логистика, хранение, приёмка, возвраты, к перечислению).
- Парсинг полей изолирован в одной функции (раздел 13: с 15.07.2026 формат меняется).
- article пока = nm_id (строкой); связка nm_id→vendorCode→МойСклад — следующим шагом.

Запуск:  ./venv/bin/python collectors/wb.py
"""
import os
import sys
import time
import pathlib
from collections import defaultdict

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
REPORT_URL = "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod"
STOCKS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}


def _token(account):
    t = os.getenv(TOKEN_ENV[account])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан в .env")
    return t


def fetch_report(account, date_from, date_to, limit=100000):
    """Финотчёт за период, пагинация по rrdid. Лимит WB строгий — обрабатываем 429."""
    H = {"Authorization": _token(account)}
    out, rrdid = [], 0
    while True:
        r = requests.get(REPORT_URL, headers=H, timeout=300, params={
            "dateFrom": date_from, "dateTo": date_to, "limit": limit, "rrdid": rrdid})
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "60")) + 1)
            continue
        r.raise_for_status()
        batch = r.json() or []
        if not batch:
            break
        out.extend(batch)
        rrdid = batch[-1]["rrd_id"]
        print(f"  [wb fetch] +{len(batch)} (всего {len(out)}) rrdid→{rrdid}", flush=True)
        if len(batch) < limit:
            break
        time.sleep(2)
    return out


def load_raw(account, rows, date_from, date_to):
    recs = [{"account": account, "rrd_id": r.get("rrd_id"),
             "period_from": date_from, "period_to": date_to,
             "payload": psycopg2.extras.Json(r)} for r in rows if r.get("rrd_id") is not None]
    return db.upsert("raw_wb_report", recs, conflict_cols=["account", "rrd_id"])


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


def normalize_sales(account, rows, date_from, date_to):
    """Агрегат по nm_id за период → sales. Поля WB → модель раздела 5."""
    agg = defaultdict(lambda: defaultdict(float))
    spp, price = defaultdict(list), defaultdict(list)
    for r in rows:
        nm = r.get("nm_id")
        if nm is None:
            continue
        a = agg[nm]
        op = r.get("supplier_oper_name")
        if op == "Продажа":
            a["qty"] += _f(r, "quantity")
            a["revenue_buyer"] += _f(r, "retail_amount")
            a["commission"] += _f(r, "retail_amount") - _f(r, "ppvz_for_pay")
            if _f(r, "ppvz_spp_prc"):
                spp[nm].append(_f(r, "ppvz_spp_prc"))
            if _f(r, "retail_price_withdisc_rub"):
                price[nm].append(_f(r, "retail_price_withdisc_rub"))
        elif op == "Возврат":
            a["qty"] -= _f(r, "quantity")
            a["returns_sum"] += _f(r, "retail_amount")
            a["revenue_buyer"] -= _f(r, "retail_amount")
            a["commission"] -= _f(r, "retail_amount") - _f(r, "ppvz_for_pay")
        a["to_pay"] += _f(r, "ppvz_for_pay")
        a["logistics"] += _f(r, "delivery_rub")
        a["logistics_cnt"] += _f(r, "delivery_amount")
        a["storage"] += _f(r, "storage_fee")
        a["acceptance"] += _f(r, "acceptance")
        a["other"] += _f(r, "deduction") + _f(r, "penalty")
    recs = []
    for nm, a in agg.items():
        pr = price[nm]
        recs.append({
            "article": str(nm), "platform": "wb", "account": account,
            "period_from": date_from, "period_to": date_to, "granularity": "month",
            "qty": a["qty"],
            "our_price": (sum(pr) / len(pr)) if pr else None,
            "buyer_price": (a["revenue_buyer"] / a["qty"]) if a["qty"] else None,
            "revenue_buyer": a["revenue_buyer"], "to_pay": a["to_pay"],
            "commission": a["commission"], "logistics": a["logistics"],
            "logistics_cnt": a["logistics_cnt"], "returns_sum": a["returns_sum"],
            "storage": a["storage"], "acceptance": a["acceptance"], "other": a["other"],
        })
    db.upsert("sales", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to", "granularity"])
    # средний СПП по контрольному SKU — для валидации (не пишем в БД, только вернём)
    ctrl_spp = None
    if 216421567 in spp and spp[216421567]:
        ctrl_spp = sum(spp[216421567]) / len(spp[216421567])
    return len(recs), ctrl_spp


def collect_stocks(account, captured_at, since="2025-01-01"):
    """Остатки на складах WB (FBO) → wb_stocks. Снимок на captured_at."""
    r = requests.get(STOCKS_URL, headers={"Authorization": _token(account)},
                     params={"dateFrom": since}, timeout=120)
    r.raise_for_status()
    rows = r.json() or []
    recs = [{
        "account": account, "nm_id": x.get("nmId"), "vendor_code": x.get("supplierArticle"),
        "warehouse": x.get("warehouseName"), "quantity": x.get("quantity"),
        "quantity_full": x.get("quantityFull"), "in_way_to_client": x.get("inWayToClient"),
        "in_way_from_client": x.get("inWayFromClient"), "brand": x.get("brand"),
        "subject": x.get("subject"), "captured_at": captured_at,
    } for x in rows if x.get("nmId") is not None]
    db.upsert("wb_stocks", recs, conflict_cols=["account", "nm_id", "warehouse", "captured_at"])
    return len(recs)


def main(account="wb_acc1", date_from="2026-05-01", date_to="2026-05-31"):
    print(f"WB {account} {date_from}..{date_to}", flush=True)
    rows = fetch_report(account, date_from, date_to)
    n_raw = load_raw(account, rows, date_from, date_to)
    n_sales, ctrl_spp = normalize_sales(account, rows, date_from, date_to)
    print(f"\nИтого: строк отчёта {len(rows)} → raw {n_raw}, sales(nm_id) {n_sales}", flush=True)
    if ctrl_spp is not None:
        print(f"Контроль СПП по nm_id 216421567: средн. {ctrl_spp:.2f}% (ожидаем ≈28.84%)", flush=True)


if __name__ == "__main__":
    main()
