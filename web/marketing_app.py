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
