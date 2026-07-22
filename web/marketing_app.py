"""Автономный mkt-сервис: страница /marketing + API /api/marketing.

Вынесен из web/app.py НАМЕРЕННО: общий app.py — это грязный чекаут ветки eng,
который постоянно переписывается чужой сессией, и дописанный туда роут не выживает
(его затирают при сохранении их версии файла). Отдельный процесс на своём порту
(:8092), nginx направляет на него /marketing и /api/marketing. Читает ту же общую БД
(mkt_sku_economics) и web/static/marketing.html. Домен mkt, только чтение витрины.

Запуск: systemd-юнит mp-marketing.service (uvicorn web.marketing_app:app --port 8092).
"""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402  (core.db сам грузит .env из BASE_DIR)

STATIC = BASE_DIR / "web" / "static"
app = FastAPI(title="mp-analytics · Маркетинг")


@app.get("/marketing", response_class=HTMLResponse)
def marketing_page():
    return (STATIC / "marketing.html").read_text(encoding="utf-8")


@app.get("/margin-control", response_class=HTMLResponse)
def margin_control_page():
    return (STATIC / "margin_control.html").read_text(encoding="utf-8")


@app.get("/api/margin-control")
def margin_control(account: str = "wb_acc1", view: str = "below", q: str = "",
                   date: str = "", limit: int = 500):
    """Ежедневный контроль маржи на ЖИВОЙ себестоимости TheCartridge (mkt_margin_control, домен mkt).
    view: below (ниже порога) | negative | no_lu (продаём без ЛУ) | all. Читает снимок последнего дня
    (или date=YYYY-MM-DD). Маржа-live = to_pay − логистика − хранение − приёмка − buy_price_live,
    рядом FIFO-себест из отгрузок МС и расхождение cogs_delta (buy_price = «почём купим сегодня»)."""
    day = date or db.query(
        "SELECT max(captured_date)::text d FROM mkt_margin_control WHERE account=%s", (account,))[0]["d"]
    where, params = ["account=%s", "captured_date=%s"], [account, day]
    if view == "below":
        where.append("below_threshold")
    elif view == "negative":
        where.append("is_negative")
    elif view == "no_lu":
        where.append("buy_status='no_lu'")
    if q:
        where.append("(nm_id::text LIKE %s OR vendor_code ILIKE %s OR subject ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like]
    order = ("margin_own_live ASC NULLS LAST" if view in ("below", "negative")
             else "nm_id" if view == "no_lu" else "margin_own_live ASC NULLS LAST")
    rows = db.query(f"""
      SELECT nm_id, vendor_code, external_code, map_source, subject,
             our_price, buyer_price, to_pay_u, logistics_u,
             buy_price_live, buy_status, fifo_cogs_u, cogs_delta,
             net_live, margin_own_live, net_fifo, margin_own_fifo,
             below_threshold, is_negative
      FROM mkt_margin_control
      WHERE {' AND '.join(where)}
      ORDER BY {order}
      LIMIT %s
    """, tuple(params) + (limit,))
    summ = db.query("""
      SELECT count(*) tot,
             count(*) FILTER (WHERE buy_status='ok')       live_ok,
             count(*) FILTER (WHERE buy_status='no_lu')    no_lu,
             count(*) FILTER (WHERE buy_status='unmapped') unmapped,
             count(*) FILTER (WHERE map_source='prefix' AND buy_status='ok') prefix_mapped,
             count(*) FILTER (WHERE below_threshold)       below,
             count(*) FILTER (WHERE is_negative)           negative,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_own_live)
               FILTER (WHERE buy_status='ok')              med_margin_live,
             max(threshold_pct)                            threshold,
             count(*) FILTER (WHERE cogs_delta>0)          delta_pos,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY cogs_delta)
               FILTER (WHERE buy_status='ok')              med_delta
      FROM mkt_margin_control WHERE account=%s AND captured_date=%s
    """, (account, day))[0]
    return {"summary": summ, "rows": rows, "date": day, "view": view}


@app.get("/api/marketing")
def marketing(account: str = "wb_acc1", q: str = "", sort: str = "trail_qty",
              only_sold: int = 0, limit: int = 300):
    """Витрина юнит-экономики SKU (mkt_sku_economics, домен mkt): 3-ценовой стек, форвард net/маржа
    через payout-ratio, KPI-маржа 25% от нашей цены, 25%-лимит и безубыток акции, трейлинг-факт,
    сценарий по глубине акции. Для решений «на какие SKU поднимать ставку рекламы»."""
    where, params = ["account=%s"], [account]
    if q:
        where.append("(nm_id::text LIKE %s OR vendor_code ILIKE %s OR subject ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like]
    if only_sold:
        where.append("trail_qty > 0")
    sort_col = {"trail_qty": "trail_qty", "net_u": "net_u", "margin_own": "margin_pct_own",
                "breakeven": "promo_breakeven_pct", "limit25": "promo_limit_25",
                "promo": "promo_pct"}.get(sort, "trail_qty")
    rows = db.query(f"""
      SELECT nm_id, vendor_code, subject,
             price_before_promo, promo_pct, promo_price, buyer_price, spp_pct_card,
             payout_ratio, payout_source, cogs_u, cogs_source,
             to_pay_u, net_u, margin_pct_own, margin_pct_wb,
             promo_breakeven_pct, promo_limit_25,
             trail_qty, trail_realized_u, last_sale_date::text last_sale_date, days_since_sale,
             sold_flag, net_u_actual, margin_pct_wb_actual, margin_pct_own_actual, scenario_promo
      FROM mkt_sku_economics
      WHERE {' AND '.join(where)}
      ORDER BY {sort_col} DESC NULLS LAST, trail_qty DESC NULLS LAST
      LIMIT %s
    """, tuple(params) + (limit,))
    summ = db.query("""
      SELECT count(*) tot,
             count(*) FILTER (WHERE trail_qty>0)                              sold,
             count(*) FILTER (WHERE cogs_u IS NOT NULL)                        with_cogs,
             count(*) FILTER (WHERE margin_pct_own>=25 AND trail_qty>0)        kpi_ok,
             count(*) FILTER (WHERE margin_pct_own<25 AND trail_qty>0)         kpi_bad,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_pct_own) FILTER (WHERE trail_qty>0) med_margin_own,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY payout_ratio)         med_payout,
             max(period_econ)::text                                            period_econ
      FROM mkt_sku_economics WHERE account=%s
    """, (account,))[0]
    return {"summary": summ, "rows": rows, "target_margin": 25}
