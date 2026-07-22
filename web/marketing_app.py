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
    view: below (ниже порога) | negative | no_price (нет цены у платформы) | all. Читает снимок
    последнего дня (или date=YYYY-MM-DD). Маржа-live = to_pay − логистика − хранение − приёмка −
    buy_price_live; рядом FIFO из отгрузок МС и cogs_delta (buy_price = «почём купим сегодня»).
    buy_status: ok(цена сегодня) | stale(послед.известная) | no_price | unmapped."""
    day = date or db.query(
        "SELECT max(captured_date)::text d FROM mkt_margin_control WHERE account=%s", (account,))[0]["d"]
    where, params = ["mc.account=%s", "mc.captured_date=%s"], [account, day]
    if view == "below":
        where.append("mc.below_threshold")
    elif view == "negative":
        where.append("mc.is_negative")
    elif view == "no_price":
        where.append("mc.buy_status='no_price'")
    if q:
        where.append("(mc.nm_id::text LIKE %s OR mc.vendor_code ILIKE %s OR c.title ILIKE %s OR mc.subject ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like, like]
    order = ("mc.nm_id" if view == "no_price" else "mc.margin_own_live ASC NULLS LAST")
    # платформенные расходы одной суммой = комиссия+СПП+логистика+хранение+приёмка = наша цена − к перечислению
    # + логистика + хранение + приёмка (себестоимость сюда НЕ входит — показываем её отдельной колонкой).
    rows = db.query(f"""
      SELECT mc.nm_id, mc.vendor_code, mc.external_code, mc.map_source,
             COALESCE(c.title, mc.subject) AS title,
             mc.our_price, mc.buyer_price, mc.to_pay_u, mc.logistics_u,
             (mc.our_price - mc.to_pay_u + COALESCE(mc.logistics_u,0)
                + COALESCE(mc.storage_u,0) + COALESCE(mc.accept_u,0)) AS platform_costs,
             mc.buy_price_live, mc.buy_status, mc.price_date::text price_date, mc.fifo_cogs_u, mc.cogs_delta,
             mc.net_live, mc.margin_own_live, mc.net_fifo, mc.margin_own_fifo,
             mc.below_threshold, mc.is_negative
      FROM mkt_margin_control mc
      LEFT JOIN wb_cards c ON c.account = mc.account AND c.nm_id = mc.nm_id
      WHERE {' AND '.join(where)}
      ORDER BY {order}
      LIMIT %s
    """, tuple(params) + (limit,))
    summ = db.query("""
      SELECT count(*) tot,
             count(*) FILTER (WHERE buy_status IN ('ok','stale')) live_ok,
             count(*) FILTER (WHERE buy_status='stale')    stale,
             count(*) FILTER (WHERE buy_status='no_price')  no_price,
             count(*) FILTER (WHERE buy_status='unmapped') unmapped,
             count(*) FILTER (WHERE map_source='prefix' AND buy_status IN ('ok','stale')) prefix_mapped,
             count(*) FILTER (WHERE below_threshold)       below,
             count(*) FILTER (WHERE is_negative)           negative,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_own_live)
               FILTER (WHERE buy_status IN ('ok','stale'))  med_margin_live,
             max(threshold_pct)                            threshold,
             count(*) FILTER (WHERE cogs_delta>0)          delta_pos,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY cogs_delta)
               FILTER (WHERE buy_status IN ('ok','stale'))  med_delta
      FROM mkt_margin_control WHERE account=%s AND captured_date=%s
    """, (account, day))[0]
    return {"summary": summ, "rows": rows, "date": day, "view": view}


@app.get("/api/marketing")
def marketing(account: str = "wb_acc1", q: str = "", sort: str = "trail_qty",
              only_sold: int = 0, limit: int = 300):
    """Витрина юнит-экономики SKU (mkt_sku_economics, домен mkt): 3-ценовой стек, форвард net/маржа
    через payout-ratio, KPI-маржа 25% от нашей цены, 25%-лимит и безубыток акции, трейлинг-факт,
    сценарий по глубине акции. Для решений «на какие SKU поднимать ставку рекламы»."""
    where, params = ["e.account=%s"], [account]
    if q:
        where.append("(e.nm_id::text LIKE %s OR e.vendor_code ILIKE %s OR c.title ILIKE %s OR e.subject ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if only_sold:
        where.append("e.trail_qty > 0")
    sort_col = {"trail_qty": "trail_qty", "net_u": "net_u", "margin_own": "margin_pct_own",
                "breakeven": "promo_breakeven_pct", "limit25": "promo_limit_25",
                "promo": "promo_pct"}.get(sort, "trail_qty")
    # Живая («восстановительная») себест TheCartridge + маржа-live — из mkt_margin_control (снимок
    # последнего дня, домен mkt). LEFT JOIN по (account, nm_id): показываем рядом с FIFO-себест.
    rows = db.query(f"""
      SELECT e.nm_id, e.vendor_code, e.subject, COALESCE(c.title, e.subject) AS title,
             e.price_before_promo, e.promo_pct, e.promo_price, e.buyer_price, e.spp_pct_card,
             e.payout_ratio, e.payout_source, e.cogs_u, e.cogs_source,
             e.to_pay_u, e.net_u, e.margin_pct_own, e.margin_pct_wb,
             e.promo_breakeven_pct, e.promo_limit_25,
             e.trail_qty, e.trail_realized_u, e.last_sale_date::text last_sale_date, e.days_since_sale,
             e.sold_flag, e.net_u_actual, e.margin_pct_wb_actual, e.margin_pct_own_actual, e.scenario_promo,
             mc.buy_price_live, mc.margin_own_live, mc.buy_status, mc.cogs_delta,
             mc.price_date::text price_date
      FROM mkt_sku_economics e
      LEFT JOIN wb_cards c
        ON c.account = e.account AND c.nm_id = e.nm_id
      LEFT JOIN mkt_margin_control mc
        ON mc.account = e.account AND mc.nm_id = e.nm_id
       AND mc.captured_date = (SELECT max(captured_date) FROM mkt_margin_control WHERE account = e.account)
      WHERE {' AND '.join(where)}
      ORDER BY e.{sort_col} DESC NULLS LAST, e.trail_qty DESC NULLS LAST
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
    live = db.query("""
      SELECT count(*) FILTER (WHERE buy_status IN ('ok','stale')) with_live
      FROM mkt_margin_control
      WHERE account=%s AND captured_date=(SELECT max(captured_date) FROM mkt_margin_control WHERE account=%s)
    """, (account, account))[0]
    summ["with_live"] = live["with_live"]
    return {"summary": summ, "rows": rows, "target_margin": 25}
