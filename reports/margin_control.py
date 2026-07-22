# поток: mkt
"""reports/margin_control.py — ежедневный контроль маржи на ЖИВОЙ себестоимости TheCartridge.

Считает по каждому WB acc1 SKU юнит-экономику на восстановительной закупке (tc_buy_price):
    net_live = to_pay_u − logistics_u − storage_u − accept_u − buy_price_live
    margin_own_live = 100 * net_live / our_price      (наша промо-цена, до СПП — KPI)
Цена/комиссия(payout)/логистика — из витрины mkt_sku_economics (fin/mkt форвард, read-only).
buy_price_live — «почём купим сегодня» (для решений), РЯДОМ держим FIFO-себест из отгрузок МС
(cogs_u) и расхождение cogs_delta — это ВТОРАЯ себестоимость, НЕ замена FIFO.

Маппинг nm→external_code: (1) путь отгрузки (nm→ms_demand_pos→ms_product, авторитетно для реально
отгружаемого товара), фолбэк (2) wb_cards.vendor_code = external_code.

Контроль «выпадаем по марже»:
  • below_threshold — margin_own_live < порога (по умолчанию 25% от нашей цены);
  • is_negative     — net_live < 0 (в отчёте красным);
  • buy_status='no_lu' — продаём без ЛУ на платформе (отдельный список), 'unmapped' — нет кода.
Формат: файл reports/data/margin_control_<дата>.{csv,txt} + краткая сводка в чат (≤50 строк).

Запуск:  ./venv/bin/python reports/margin_control.py [--threshold 25] [--date YYYY-MM-DD]
"""
import os
import sys
import csv
import datetime
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")

RAW_DIR = BASE_DIR / "reports" / "data"
ACCOUNT = "wb_acc1"
DEFAULT_THRESHOLD = 25.0


def _f(v):
    return None if v is None else float(v)


def _mapping(account):
    """nm_id → external_code. Приоритет: путь отгрузки, фолбэк vendor_code=external_code."""
    ship = {int(r["nm"]): r["ec"] for r in db.query("""
        WITH nm_ext AS (
          SELECT w.payload->>'nm_id' nm, p.external_code ec, sum(ps.qty) q
          FROM raw_wb_report w
          JOIN ms_demand_cogs d ON d.demand_name = w.payload->>'assembly_id'
          JOIN ms_demand_pos ps ON ps.demand_id = d.demand_id
          JOIN ms_product p     ON p.ms_id = ps.ms_id
          WHERE w.account=%s AND p.external_code IS NOT NULL AND w.payload->>'nm_id' ~ '^[0-9]+$'
          GROUP BY 1, 2),
        ranked AS (SELECT nm, ec, row_number() OVER (PARTITION BY nm ORDER BY q DESC) rn FROM nm_ext)
        SELECT nm::bigint nm, ec FROM ranked WHERE rn = 1
    """, (account,))}
    card = {int(r["nm"]): r["ec"] for r in db.query("""
        SELECT c.nm_id nm, p.external_code ec
        FROM wb_cards c JOIN ms_product p ON p.external_code = c.vendor_code
        WHERE c.account=%s AND p.external_code IS NOT NULL AND NOT p.archived
    """, (account,))}
    card.update(ship)   # путь отгрузки перекрывает фолбэк
    return card


def build(account=ACCOUNT, threshold=DEFAULT_THRESHOLD, on_date=None):
    day = on_date or datetime.date.today().isoformat()

    nm_ec = _mapping(account)
    # живая закупка: последняя известная цена по коду (вью tc_buy_price_latest)
    live = {r["external_code"]: (_f(r["buy_price"]), r["status"])
            for r in db.query("SELECT external_code, buy_price, status FROM tc_buy_price_latest")}

    econ = db.query("""
        SELECT nm_id, vendor_code, subject, promo_price, buyer_price, payout_ratio, to_pay_u,
               logistics_u, storage_u, accept_u, cogs_u, net_u, margin_pct_own,
               sold_flag, qty_period, days_since_sale
        FROM mkt_sku_economics WHERE account=%s
    """, (account,))

    recs = []
    for e in econ:
        nm = int(e["nm_id"])
        ec = nm_ec.get(nm)
        bp_live, status = (None, "unmapped")
        if ec and ec in live:
            bp_live, st = live[ec]
            status = "ok" if (bp_live is not None and st == "ok") else "no_lu"

        our_price = _f(e["promo_price"])
        to_pay = _f(e["to_pay_u"])
        log_u, stor_u, acc_u = _f(e["logistics_u"]), _f(e["storage_u"]), _f(e["accept_u"])
        fifo = _f(e["cogs_u"])
        payout = _f(e["payout_ratio"])
        # exact our_price recovery (to_pay = our_price*payout), фолбэк promo_price
        if to_pay is not None and payout:
            our_price = to_pay / payout

        net_live = margin_live = None
        if bp_live is not None and to_pay is not None:
            net_live = to_pay - (log_u or 0) - (stor_u or 0) - (acc_u or 0) - bp_live
            if our_price:
                margin_live = 100 * net_live / our_price
        cogs_delta = (bp_live - fifo) if (bp_live is not None and fifo is not None) else None

        below = (margin_live is not None and margin_live < threshold)
        negative = (net_live is not None and net_live < 0)

        recs.append({
            "captured_date": day, "account": account, "nm_id": nm,
            "vendor_code": e["vendor_code"], "external_code": ec, "subject": e["subject"],
            "our_price": (round(our_price, 2) if our_price else None),
            "buyer_price": _f(e["buyer_price"]),
            "payout_ratio": payout, "to_pay_u": to_pay,
            "logistics_u": log_u, "storage_u": stor_u, "accept_u": acc_u,
            "buy_price_live": bp_live, "buy_status": status,
            "fifo_cogs_u": fifo,
            "cogs_delta": (round(cogs_delta, 2) if cogs_delta is not None else None),
            "net_live": (round(net_live, 2) if net_live is not None else None),
            "margin_own_live": (round(margin_live, 2) if margin_live is not None else None),
            "net_fifo": _f(e["net_u"]),
            "margin_own_fifo": _f(e["margin_pct_own"]),
            "below_threshold": below, "is_negative": negative,
            "threshold_pct": threshold,
        })

    # снимок дня идемпотентно
    db.execute("DELETE FROM mkt_margin_control WHERE captured_date=%s AND account=%s", (day, account))
    db.upsert("mkt_margin_control", recs, conflict_cols=["captured_date", "account", "nm_id"])

    _write_report(account, day, threshold, recs)
    return recs


def _write_report(account, day, threshold, recs):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # только SKU с живой ценой считаем «в контроле»; для приоритета — сначала продающиеся/свежие
    priced = [r for r in recs if r["buy_price_live"] is not None]
    below = sorted([r for r in priced if r["below_threshold"]],
                   key=lambda r: (r["margin_own_live"] if r["margin_own_live"] is not None else 999))
    negative = [r for r in below if r["is_negative"]]
    no_lu = [r for r in recs if r["buy_status"] == "no_lu"]
    unmapped = [r for r in recs if r["buy_status"] == "unmapped"]

    # CSV — весь снимок (сырьё в файл, не в чат)
    csv_path = RAW_DIR / f"margin_control_{day}.csv"
    fields = ["nm_id", "vendor_code", "external_code", "subject", "our_price", "buyer_price",
              "to_pay_u", "logistics_u", "buy_price_live", "buy_status", "fifo_cogs_u", "cogs_delta",
              "net_live", "margin_own_live", "margin_own_fifo", "below_threshold", "is_negative",
              "sold_flag" if False else "qty_period"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c for c in fields if c != "qty_period"] + ["days_since_sale"])
        for r in sorted(recs, key=lambda r: (r["margin_own_live"] is None,
                                             r["margin_own_live"] if r["margin_own_live"] is not None else 999)):
            w.writerow([r.get("nm_id"), r.get("vendor_code"), r.get("external_code"), r.get("subject"),
                        r.get("our_price"), r.get("buyer_price"), r.get("to_pay_u"), r.get("logistics_u"),
                        r.get("buy_price_live"), r.get("buy_status"), r.get("fifo_cogs_u"), r.get("cogs_delta"),
                        r.get("net_live"), r.get("margin_own_live"), r.get("margin_own_fifo"),
                        r.get("below_threshold"), r.get("is_negative"), None])

    # TXT — краткая сводка
    txt_path = RAW_DIR / f"margin_control_{day}.txt"
    lines = []
    lines.append(f"КОНТРОЛЬ МАРЖИ {account} · {day} · порог {threshold:.0f}% от нашей цены")
    lines.append(f"SKU всего {len(recs)}: с живой закупкой {len(priced)}, "
                 f"без ЛУ {len(no_lu)}, без маппинга {len(unmapped)}")
    lines.append(f"ВЫПАДАЕМ по марже (<{threshold:.0f}%): {len(below)}  "
                 f"| из них ОТРИЦАТЕЛЬНАЯ: {len(negative)}")
    if priced:
        med = sorted(r["margin_own_live"] for r in priced if r["margin_own_live"] is not None)
        if med:
            lines.append(f"медиана маржи-live по SKU с ценой: {med[len(med)//2]:.1f}%")
    lines.append("")
    lines.append("── ТОП-20 ниже порога (по возрастанию маржи-live) ──")
    lines.append(f"{'nm_id':>10} {'марж-live':>9} {'марж-FIFO':>9} {'live':>6} {'FIFO':>6} {'Δсеб':>6} {'net':>6}  предмет")
    for r in below[:20]:
        ml = f"{r['margin_own_live']:.1f}" if r["margin_own_live"] is not None else "-"
        mf = f"{r['margin_own_fifo']:.1f}" if r["margin_own_fifo"] is not None else "-"
        flag = "‼" if r["is_negative"] else " "
        lines.append(f"{r['nm_id']:>10} {ml:>8}%{flag}{mf:>8}% {r['buy_price_live'] or 0:>6.0f} "
                     f"{r['fifo_cogs_u'] or 0:>6.0f} {r['cogs_delta'] or 0:>6.0f} {r['net_live'] or 0:>6.0f}  "
                     f"{(r['subject'] or '')[:22]}")
    lines.append("")
    lines.append(f"── Продаём БЕЗ ЛУ (no_lu): {len(no_lu)} SKU (полный список в CSV) ──")
    for r in no_lu[:10]:
        lines.append(f"{r['nm_id']:>10}  {r['vendor_code'] or '':<18} {(r['subject'] or '')[:30]}")
    txt = "\n".join(lines)
    txt_path.write_text(txt + "\n", encoding="utf-8")

    print(txt)
    print(f"\n[файлы] {csv_path.name} (весь снимок) · {txt_path.name} (сводка)")


if __name__ == "__main__":
    args = sys.argv[1:]
    thr = DEFAULT_THRESHOLD
    if "--threshold" in args:
        thr = float(args[args.index("--threshold") + 1])
    d = None
    if "--date" in args:
        d = args[args.index("--date") + 1]
    build(threshold=thr, on_date=d)
