"""rebuild_validate_cogs.py — пересобрать margin_by_sku на новом источнике COGS
(ms_demand_cogs / report-stock-byoperation) и сверить с эталоном cogs_actual + контролем nm.

Запуск ПОСЛЕ бэкофилла ms_demand_cogs.
"""
import sys
import pathlib
BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from core import db                       # noqa: E402
import reports.margin_by_sku as margin    # noqa: E402

CONTROL_NM = "216421567"


def periods(account):
    return [(str(r["period_from"]), str(r["period_to"])) for r in db.query(
        """SELECT DISTINCT period_from, period_to FROM sales
           WHERE platform='wb' AND account=%s ORDER BY period_from""", (account,))]


def rebuild():
    for acc in ("wb_acc1", "wb_acc2"):
        for df, dt in periods(acc):
            print(f"build {acc} {df}..{dt}", flush=True)
            margin.build(acc, df, dt)


def compare():
    # эталон: cogs_actual (платформа wb = Цифровой = wb_acc1)
    etalon = {str(r["month"])[:7]: float(r["cogs"]) for r in db.query(
        "SELECT month, cogs FROM cogs_actual WHERE platform='wb'")}
    print("\n=== WB Цифровой (wb_acc1): COGS дашборд vs эталон cogs_actual ===")
    print(f"  {'мес':9}{'COGS новый':>14}{'эталон':>14}{'Δ':>12}{'Δ%':>8}")
    rows = db.query("""SELECT to_char(period_from,'YYYY-MM') ym, round(sum(cogs)) cogs,
        round(sum(revenue_buyer)) rev, round(sum(net_profit)) net
        FROM margin_by_sku WHERE platform='wb' AND account='wb_acc1' GROUP BY 1 ORDER BY 1""")
    for r in rows:
        e = etalon.get(r["ym"])
        cogs = float(r["cogs"])
        d = (cogs - e) if e else None
        dp = (d / e * 100) if e else None
        es = f"{int(e):,}" if e else "—"
        ds = f"{int(d):+,}" if d is not None else "—"
        dps = f"{dp:+.1f}%" if dp is not None else ""
        print(f"  {r['ym']:9}{int(cogs):>14,}{es:>14}{ds:>12}{dps:>8}")

    print("\n=== Контроль nm", CONTROL_NM, "(май 2026, wb_acc1) ===")
    for r in db.query("""SELECT to_char(period_from,'YYYY-MM') ym, qty,
        round(revenue_buyer) rev, round(cogs) cogs, round(cogs/NULLIF(qty,0)) cpu,
        round(net_profit) net, round(margin_pct,1) mp
        FROM margin_by_sku WHERE platform='wb' AND account='wb_acc1' AND article=%s
        ORDER BY period_from""", (CONTROL_NM,)):
        print(f"  {r['ym']}: qty={r['qty']} rev={r['rev']} cogs={r['cogs']} "
              f"cogs/шт={r['cpu']} net={r['net']} маржа={r['mp']}%")
    print("  (ориентир из ARCHITECTURE: май net ≈ 3909 при 61 продаже; память: система ~4254)")


if __name__ == "__main__":
    if "--compare-only" not in sys.argv:
        rebuild()
    compare()
