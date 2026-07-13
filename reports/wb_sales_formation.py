"""reports/wb_sales_formation.py — слой sales (цены/выручка ВБ по nm_id) по МЕСЯЦУ ФОРМИРОВАНИЯ.

Исторически sales писал collectors/wb.normalize_sales по окну запроса (rr_dt), а витрина
margin_by_sku перешла на месяц формирования отчёта (create_dt) — периоды разъехались, и
дашборд смешивал модели (наша цена/цена ВБ/СПП не сходились с выручкой витрины).
Этот билдер пересобирает sales из raw_wb_report по create_dt — та же семантика полей,
что у normalize_sales: our_price = avg retail_price_withdisc_rub (наша цена, до СПП),
revenue_wb = retail_amount («Вайлдберриз реализовал», после СПП).

Запуск: ./venv/bin/python reports/wb_sales_formation.py [wb_acc1 [2026-06-01]]
Без аргументов — все месяцы обоих аккаунтов.
"""
import sys
import calendar
import pathlib
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402


def _f(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


def build(account, ym):
    """ym = 'YYYY-MM'. Пересобирает sales за месяц формирования целиком."""
    y, m = int(ym[:4]), int(ym[5:7])
    date_from = f"{ym}-01"
    date_to = f"{ym}-{calendar.monthrange(y, m)[1]:02d}"
    raw = db.query("""
        SELECT payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
               payload->>'quantity' q, payload->>'retail_price_withdisc_rub' rpw,
               payload->>'retail_amount' ra, payload->>'ppvz_for_pay' pay,
               payload->>'delivery_rub' del, payload->>'delivery_amount' dela,
               payload->>'storage_fee' st, payload->>'acceptance' acc,
               payload->>'deduction' ded, payload->>'penalty' pen
        FROM raw_wb_report
        WHERE account=%s AND to_char((payload->>'create_dt')::date,'YYYY-MM')=%s""",
                   (account, ym))
    if not raw:
        return 0
    agg = defaultdict(lambda: defaultdict(float))
    price = defaultdict(list)
    for r in raw:
        nm = r["nm"]
        if not nm:
            continue
        a = agg[nm]
        if r["op"] == "Продажа":
            a["qty"] += _f(r["q"])
            a["revenue_buyer"] += _f(r["rpw"])
            a["revenue_wb"] += _f(r["ra"])
            a["commission"] += _f(r["ra"]) - _f(r["pay"])
            if _f(r["rpw"]):
                price[nm].append(_f(r["rpw"]))
        elif r["op"] == "Возврат":
            a["qty"] -= _f(r["q"])
            a["returns_sum"] += _f(r["ra"])
            a["revenue_buyer"] -= _f(r["rpw"])
            a["revenue_wb"] -= _f(r["ra"])
            a["commission"] -= _f(r["ra"]) - _f(r["pay"])
        a["to_pay"] += _f(r["pay"])
        a["logistics"] += _f(r["del"])
        a["logistics_cnt"] += _f(r["dela"])
        a["storage"] += _f(r["st"])
        a["acceptance"] += _f(r["acc"])
        a["other"] += _f(r["ded"]) + _f(r["pen"])
    recs = []
    for nm, a in agg.items():
        pr = price[nm]
        recs.append({
            "article": nm, "platform": "wb", "account": account,
            "period_from": date_from, "period_to": date_to, "granularity": "month",
            "qty": a["qty"],
            "our_price": (sum(pr) / len(pr)) if pr else None,
            "buyer_price": (a["revenue_buyer"] / a["qty"]) if a["qty"] else None,
            "revenue_buyer": a["revenue_buyer"], "revenue_wb": a["revenue_wb"], "to_pay": a["to_pay"],
            "commission": a["commission"], "logistics": a["logistics"],
            "logistics_cnt": a["logistics_cnt"], "returns_sum": a["returns_sum"],
            "storage": a["storage"], "acceptance": a["acceptance"], "other": a["other"],
        })
    # месяц пересобирается целиком — старые nm не должны залипать
    db.execute("""DELETE FROM sales WHERE platform='wb' AND account=%s
                  AND period_from=%s AND granularity='month'""", (account, date_from))
    db.upsert("sales", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to", "granularity"])
    print(f"  sales {account} {ym} (по формированию): {len(recs)} nm_id", flush=True)
    return len(recs)


def build_all(account):
    months = [r["ym"] for r in db.query("""
        SELECT DISTINCT to_char((payload->>'create_dt')::date,'YYYY-MM') ym
        FROM raw_wb_report WHERE account=%s ORDER BY 1""", (account,))]
    for ym in months:
        build(account, ym)


def main():
    if len(sys.argv) > 1:
        acc = sys.argv[1]
        if len(sys.argv) > 2:
            build(acc, sys.argv[2][:7])
        else:
            build_all(acc)
    else:
        for acc in ("wb_acc1", "wb_acc2"):
            build_all(acc)


if __name__ == "__main__":
    main()
