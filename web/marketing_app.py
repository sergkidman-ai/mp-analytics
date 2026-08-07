"""Автономный mkt-сервис: страница /marketing + API /api/marketing.

Вынесен из web/app.py НАМЕРЕННО: общий app.py — это грязный чекаут ветки eng,
который постоянно переписывается чужой сессией, и дописанный туда роут не выживает
(его затирают при сохранении их версии файла). Отдельный процесс на своём порту
(:8092), nginx направляет на него /marketing и /api/marketing. Читает ту же общую БД
(mkt_sku_economics) и web/static/marketing.html. Домен mkt, только чтение витрины.

Запуск: systemd-юнит mp-marketing.service (uvicorn web.marketing_app:app --port 8092).
"""
import os
import sys
import json
import datetime
from pathlib import Path

import requests
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402  (core.db сам грузит .env из BASE_DIR)

STATIC = BASE_DIR / "web" / "static"
app = FastAPI(title="mp-analytics · Маркетинг")

# ── Управление ставками WB ────────────────────────────────────────────────────────────────────
# Ставка WB — CPC per-nmID (не на кампанию), шаг 1 коп. Геттера текущей ставки у WB НЕТ →
# «текущую» реконструируем как факт.CPC=расход/клики (wb_ad_nm); пробелы заполняются вручную.
WB_ADS_HOST = "https://advert-api.wildberries.ru"
WB_BID_FLOOR = 7.3   # пол CPC поиска (реко 5.54); память project_mp_wb_ads_endpoint
# ЖИВАЯ запись ставки в WB. Контракт PATCH /api/advert/v1/bids ПОДТВЕРЖДЁН пробой 2026-08-04 на
# приостановленной кампании 35701146 (200, значение в КОПЕЙКАХ, placement="search"; "combined" ВБ
# отвергает для аукциона). Геттера ставки нет, DELETE нет (405 Allow=PATCH). Включено по одобренному
# плану Сергея (проба ставки+снятия). Снятие nmID: API у ВБ НЕТ → деградирует в очередь (ручное ЛК).
WB_BID_LIVE_ENABLED = True
WB_ADS_TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ADS_ACC1"}   # write-токен «Продвижение», только acc1
# Подтверждённый контракт смены ставки (значение bid_kopecks — в копейках; 7.3₽ = 730):
#   PATCH {WB_ADS_HOST}/api/advert/v1/bids  Authorization: <token>
#   {"bids":[{"advert_id":ADV,"nm_bids":[{"nm_id":NM,"bid_kopecks":V,"placement":"search"}]}]}


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


# ══ OZON: юнит-экономика · контроль маржи · ставки ═══════════════════════════════════════════════
# Только чтение. На Ozon эти страницы НИЧЕГО не отправляют (CLAUDE.md, габариты п.7 и общее
# правило: наличие данных ≠ разрешение на запись). Ставки Ozon — просмотр снимка, не управление.

OZ_ACCOUNTS = ["oz_acc1", "oz_acc2"]


@app.get("/ozon-economics", response_class=HTMLResponse)
def ozon_economics_page():
    return (STATIC / "ozon_economics.html").read_text(encoding="utf-8")


@app.get("/ozon-margin-control", response_class=HTMLResponse)
def ozon_margin_control_page():
    return (STATIC / "ozon_margin_control.html").read_text(encoding="utf-8")


@app.get("/ozon-bids", response_class=HTMLResponse)
def ozon_bids_page():
    return (STATIC / "ozon_bids.html").read_text(encoding="utf-8")


@app.get("/api/ozon-economics")
def ozon_economics(account: str = "oz_acc1", q: str = "", limit: int = 300):
    """Юнит-экономика Ozon за последний полный месяц: витрина fin `margin_by_sku` (read-only)
    + штуки и цена покупателя из постингов + зона индекса цен. У Ozon в margin_by_sku
    qty = NULL by design, поэтому штуки считаем сами по доставленным постингам месяца."""
    per = db.query("""
      SELECT max(period_from)::text f, max(period_to)::text t FROM margin_by_sku
      WHERE platform='ozon' AND account=%s AND period_from < date_trunc('month', current_date)
    """, (account,))[0]
    if not per["f"]:
        return {"summary": {}, "rows": [], "period": None}
    where, params = ["m.platform='ozon'", "m.account=%s", "m.period_from=%s"], [account, per["f"]]
    if q:
        where.append("(m.article ILIKE %s OR p.offer_id ILIKE %s OR p.name ILIKE %s)")
        params += [f"%{q}%"] * 3
    rows = db.query(f"""
      WITH s AS (
        SELECT fd->>'product_id' sku,
               sum((fd->>'quantity')::int)                                    qty,
               sum((fd->>'price')::numeric * (fd->>'quantity')::int)          our_sum,
               sum((fd->>'customer_price')::numeric * (fd->>'quantity')::int) cust_sum
        FROM raw_ozon_posting rp,
             LATERAL jsonb_array_elements(
                 coalesce(rp.payload->'financial_data'->'products','[]'::jsonb)) fd
        WHERE rp.account=%s AND rp.status='delivered'
          AND rp.in_process_at >= %s::date AND rp.in_process_at < %s::date + interval '1 day'
        GROUP BY 1)
      SELECT m.article sku, p.offer_id, p.name,
             s.qty, m.revenue_buyer, m.cogs, m.commission, m.logistics, m.storage,
             m.returns_sum, m.other, m.net_profit, m.margin_pct, m.commission_pct,
             s.our_sum / nullif(s.qty,0)  our_price_u,
             s.cust_sum / nullif(s.qty,0) buyer_price_u,
             b.color_index, b.external_index
      FROM margin_by_sku m
      LEFT JOIN s ON s.sku = m.article
      LEFT JOIN LATERAL (
        SELECT offer_id, name FROM ozon_product op
        WHERE op.account=m.account AND op.sku::text=m.article
        ORDER BY updated_at DESC NULLS LAST LIMIT 1) p ON true
      LEFT JOIN LATERAL (
        SELECT color_index, external_index FROM mkt_ozon_buyer_price bp
        WHERE bp.account=m.account AND bp.offer_id=p.offer_id
        ORDER BY snapshot_date DESC LIMIT 1) b ON true
      WHERE {' AND '.join(where)}
      ORDER BY m.revenue_buyer DESC NULLS LAST
      LIMIT %s
    """, (account, per["f"], per["t"]) + tuple(params) + (limit,))
    summ = db.query("""
      SELECT count(*) sku_n, sum(revenue_buyer) revenue, sum(cogs) cogs,
             sum(net_profit) net,
             100 * sum(net_profit) / nullif(sum(revenue_buyer),0) margin_pct,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_pct) med_margin
      FROM margin_by_sku
      WHERE platform='ozon' AND account=%s AND period_from=%s
    """, (account, per["f"]))[0]
    ads = db.query("""
      SELECT sum(spend) spend FROM ozon_ads WHERE account=%s AND period=%s
    """, (account, per["f"]))[0]
    summ["ads_spend"] = ads["spend"]
    summ["drr"] = (float(ads["spend"]) * 100 / float(summ["revenue"])
                   if ads["spend"] and summ["revenue"] else None)
    return {"summary": summ, "rows": rows, "period": {"from": per["f"], "to": per["t"]},
            "account": account}


@app.get("/api/ozon-margin-control")
def ozon_margin_control(account: str = "oz_acc1", view: str = "below", q: str = "",
                        date: str = "", limit: int = 500):
    """Контроль маржи Ozon (`mkt_ozon_margin_control`, домен mkt).
    view: below (ниже порога) | negative | can (выход в зелёную зону укладывается в KPI)
        | cant (не укладывается) | no_cogs | all.
    KPI-база — НАША цена (база комиссии), не цена покупателя. `discount_limit_pct` — на сколько
    процентов можем упасть, не пробив порог; `target_discount_pct` — сколько требует зелёная зона."""
    day = date or db.query(
        "SELECT max(captured_date)::text d FROM mkt_ozon_margin_control WHERE account=%s",
        (account,))[0]["d"]
    if not day:
        return {"summary": {}, "rows": [], "date": None, "view": view}
    where, params = ["account=%s", "captured_date=%s"], [account, day]
    view_sql = {"below": "below_threshold", "negative": "is_negative",
                "can": "verdict='можно_снижать'", "cant": "verdict='не_укладывается'",
                "no_cogs": "cogs_u IS NULL"}
    if view in view_sql:
        where.append(view_sql[view])
    if q:
        where.append("(offer_id ILIKE %s OR sku::text LIKE %s OR name ILIKE %s)")
        params += [f"%{q}%"] * 3
    order = ("discount_limit_pct DESC NULLS LAST" if view == "can"
             else "margin_own_live ASC NULLS LAST")
    rows = db.query(f"""
      SELECT offer_id, sku, name, our_price, buyer_price, payout_ratio, payout_source, to_pay_u,
             (coalesce(logistics_u,0)+coalesce(storage_u,0)+coalesce(accept_u,0)
              +coalesce(returns_u,0)+coalesce(other_u,0)) platform_costs,
             logistics_u, other_u, cost_source,
             buy_price_live, buy_status, buy_map_source, price_date::text price_date,
             fifo_cogs_u, cogs_delta, cogs_u, cogs_source,
             net_live, margin_own_live, margin_own_fifo,
             below_threshold, is_negative, threshold_pct,
             price_at_threshold, discount_limit_pct,
             color_index, external_index, price_for_target, target_discount_pct, verdict
      FROM mkt_ozon_margin_control
      WHERE {' AND '.join(where)}
      ORDER BY {order}
      LIMIT %s
    """, tuple(params) + (limit,))
    summ = db.query("""
      SELECT count(*) tot,
             count(*) FILTER (WHERE margin_own_live IS NOT NULL)   with_margin,
             count(*) FILTER (WHERE below_threshold)               below,
             count(*) FILTER (WHERE is_negative)                   negative,
             count(*) FILTER (WHERE cogs_u IS NULL)                no_cogs,
             count(*) FILTER (WHERE buy_status='stale')            stale,
             count(*) FILTER (WHERE verdict='можно_снижать')       can_cut,
             count(*) FILTER (WHERE verdict='не_укладывается')     cant_cut,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_own_live) med_margin,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY discount_limit_pct) med_limit,
             max(threshold_pct) threshold
      FROM mkt_ozon_margin_control WHERE account=%s AND captured_date=%s
    """, (account, day))[0]
    return {"summary": summ, "rows": rows, "date": day, "view": view, "account": account}


@app.get("/api/ozon-bids")
def ozon_bids(account: str = "oz_acc1", q: str = "", limit: int = 300):
    """Ставки и кампании Ozon — ТОЛЬКО просмотр. Кампании из `ozon_ads` (месячные), ставки
    по SKU из `ozon_bids` (снимок Performance API). ALL_SKU_PROMO («Оплата за заказ») ставок
    по SKU не имеет by design, и выручка по нему в API не отдаётся — отсюда ad_revenue=0."""
    per = db.query("SELECT max(period)::text p FROM ozon_ads WHERE account=%s", (account,))[0]["p"]
    camps = db.query("""
      SELECT campaign_id, title, adv_type, pay_model, state, spend, views, clicks,
             ad_revenue, sold,
             CASE WHEN clicks>0 THEN spend/clicks END cpc,
             CASE WHEN ad_revenue>0 THEN 100*spend/ad_revenue END drr
      FROM ozon_ads WHERE account=%s AND period=%s ORDER BY spend DESC NULLS LAST
    """, (account, per)) if per else []
    snap = db.query("SELECT max(captured_at)::text d FROM ozon_bids WHERE account=%s",
                    (account,))[0]["d"]
    where, params = ["b.account=%s", "b.captured_at=%s"], [account, snap]
    if q:
        where.append("(b.title ILIKE %s OR b.sku::text LIKE %s OR b.campaign_title ILIKE %s)")
        params += [f"%{q}%"] * 3
    bids = db.query(f"""
      SELECT b.campaign_id, b.campaign_title, b.adv_type, b.sku, b.title, b.bid, b.target_cir,
             m.our_price, m.margin_own_live, m.discount_limit_pct, m.color_index
      FROM ozon_bids b
      -- витрина ведётся по offer_id, ставки — по sku: где sku в витрине нет,
      -- добираем через справочник товаров (ozon_product), иначе теряем треть ставок
      LEFT JOIN LATERAL (
        SELECT our_price, margin_own_live, discount_limit_pct, color_index
        FROM mkt_ozon_margin_control mc
        WHERE mc.account=b.account
          AND (mc.sku::text=b.sku::text
               OR mc.offer_id IN (SELECT p.offer_id FROM ozon_product p
                                  WHERE p.account=b.account AND p.sku::text=b.sku::text))
        ORDER BY captured_date DESC LIMIT 1) m ON true
      WHERE {' AND '.join(where)}
      ORDER BY b.bid DESC NULLS LAST
      LIMIT %s
    """, tuple(params) + (limit,)) if snap else []
    return {"period": per, "campaigns": camps, "bids": bids, "bids_date": snap,
            "account": account, "readonly": True}


# ══ Управление ставками WB ══════════════════════════════════════════════════════════════════════

@app.get("/wb-bids", response_class=HTMLResponse)
def wb_bids_page():
    return (STATIC / "wb_bids.html").read_text(encoding="utf-8")


def _jam_baseline(account, nm_id):
    """Свежий срез Джема по nm (позиция/показы/заказы) — baseline последствий смены ставки."""
    r = db.query("""
      SELECT period_start::text base_date, avg_position, open_card, orders
      FROM wb_search_report
      WHERE account=%s AND nm_id=%s
      ORDER BY period_start DESC LIMIT 1
    """, (account, nm_id))
    return r[0] if r else {"base_date": None, "avg_position": None, "open_card": None, "orders": None}


def _bid_log(rec):
    """Append-only вставка строки в журнал wb_bid_log (без ON CONFLICT — у таблицы serial PK)."""
    cols = list(rec.keys())
    sql = (f"INSERT INTO wb_bid_log ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    db.execute(sql, [rec[c] for c in cols])


# ── Маржа-гейтед вид (что делать со ставкой: растить / снять / чистить) ──────────────────────────
# Логика вместо бесполезной сортировки по позиции Джема: решение по КАЖДОМУ рекламируемому SKU.
# Маржа-гейт = 25% (KPI Сергея от НАШЕЙ цены до СПП). Потолок ДРР = 10% (задан Сергеем).
#   МАРЖА — ЖИВАЯ (mkt_margin_control, SELECT-only): ТЕКУЩАЯ цена × себест «купим сегодня» (TheCartridge).
#   Ретроспективную margin_by_sku тут НЕ используем как гейт: реклама крутит сегодняшние цены/акции,
#   а помесячная маржа врёт на тонких месяцах (напр. 216421567 июль qty=1 → −22%, а живая +34%).
#   РЕКЛАМНЫЕ агрегаты (расход/заказы/ДРР/клики) — за ПОСЛЕДНИЙ ПОЛНЫЙ месяц (август MTD тонкий).
#   «Продавался» (активность) — qty>0 в margin_by_sku за ПОСЛЕДНИЕ 3 ПОЛНЫХ месяца (не 1): для редких
#   картриджей один месяц врёт (продаётся раз в квартал → ложно в ⚫ dead). Реклама без продаж за
#   квартал = ⚫ жжёт расход впустую → «балласт-снять».
#   ЛОВУШКА ЦЕНЫ: маржа живая (по текущей цене), а активность историческая. Многие продавались ТОЛЬКО
#   на промо-цене (у acc1 77% рекл. SKU сейчас дороже своей продажной цены, в среднем +93%). Такие
#   нельзя слепо «держать»: сравниваем истор. цену продажи (revenue/qty) с текущей живой; если цена
#   выросла ≥25% и реклама дала 0 заказов → 🟣 «цена выросла — тест» (не keep), кандидат в бюджет-тест.
#   СКЛЕЙКА (imtID из raw_wb_card_content) — приоритет теста: если соседи по склейке продаются, тест
#   дешёвый (органика тащит); если склейка мёртвая — всю ставку несёт реклама.
# Живая запись в WB тут НЕ участвует — это картина для отбора.
WB_MARGIN_GATE = 25.0    # % маржи от нашей цены — ниже = не тянем в рекламе (кроме редких/наборов)
WB_DRR_CEIL = 10.0       # % ДРР — потолок-СТОП: рекомендация не заводит прогнозный ДРР выше него
WB_STEP_PCT = 10.0       # % шаг повышения ставки за раз (мягко: +10%, не прыжок к потолку — риск слить бюджет)
WB_STEP_DAYS = 2         # дней между шагами — поднял, ждём реакции столько дней, потом «пора оценить»
WB_TEST_CAP = 20.0       # ₽ потолок ставки тест-фронта: 🧪 карточку тянем +10%/день до этого CPC; так и не
#                          дала рекл-кликов к потолку → выходит из теста назад на пол (не жжём дальше)
WB_ACTIVITY_MONTHS = 3   # окно «продавался» — последние N полных месяцев (вкл. decision-месяц)
WB_REPRICE_DRIFT = 25.0  # % роста текущей цены над истор. продажной → активность отравлена промо
WB_REPRICE_MIN_QTY = 5   # мин. продаж за окно, чтобы истор. цена была надёжной (иначе q3=1-2 = шум)

def _decision_period(account):
    r = db.query("SELECT max(period)::text p FROM wb_ad_nm "
                 "WHERE account=%s AND period < date_trunc('month', CURRENT_DATE)", (account,))
    return r[0]["p"] if r and r[0]["p"] else None

def _verdict(sold, mp, drr, spend, ad_orders, repriced=False, test_ready=False):
    """Вердикт по SKU. sold=продавался за 3 мес (qty>0); mp=маржа live%; drr=ДРР%;
    spend=расход рекламы; ad_orders=заказы, ПРИПИСАННЫЕ рекламе; repriced=цена выросла ≥25% над истор.;
    test_ready=кандидат тест-фронта (есть активная РК, рекламой не тестирован, ставка ниже потолка теста).
      ⚫ dead      — не продавался за квартал, но реклама жгла бюджет → балласт-снять
      ⚪ hold      — нет сигнала (нет расхода) или маржа неизвестна
      🔴 cut       — продавался, но маржа live ниже гейта 25% → снять с рекламы / поднять цену
      🧪 test      — маржа ок, продавался, 0 рекл-заказов, но рекламой НЕ тестирован и ставка на дне →
                     широкий тест +10%/день, пока не пойдут показы/заказы или не упрётся в потолок теста
      🟣 repriced  — маржа ок, продавался, реклама дала 0 заказов, НО цена выросла ≥25% над продажной
                     (продажи были на промо), и в тест-фронт не попал → бюджет-тест при текущей цене
      🔵 keep      — маржа ок, продавался ≈ по текущей цене, реклама дала 0 заказов → органика, держать
      🟡 expensive — маржа ок, реклама конвертит, но ДРР ≥ потолка → ставку вниз
      🟢 grow      — маржа ок, реклама конвертит, ДРР ниже потолка → есть куда поднимать ставку"""
    if not sold:
        return ("dead", "⚫ жжёт впустую") if (spend or 0) > 0 else ("hold", "⚪ нет сигнала")
    if mp is None:
        return ("hold", "⚪ маржа ?")
    if mp < WB_MARGIN_GATE:
        return ("cut", "🔴 снять / поднять цену")
    if (ad_orders or 0) == 0:
        if test_ready:
            return ("test", "🧪 тест-фронт")
        if repriced:
            return ("repriced", "🟣 цена выросла — тест")
        return ("keep", "🔵 держать на полу")
    if drr is not None and drr >= WB_DRR_CEIL:
        return ("expensive", "🟡 дорого — ставку вниз")
    return ("grow", "🟢 растить")

def _rec_cpc(verdict, cpc, drr):
    """Рекомендованная ставка — ОДИН мягкий шаг, не прыжок к потолку (иначе 40₽/клик за пару часов
    выкликают бюджет). Прогноз: ДРР ≈ линейно от CPC.
    • grow (ДРР<потолка): шаг +WB_STEP_PCT% (×1.10), но НЕ дальше потолка — cap = потолок/ДРР.
      Итог: cpc × min(1+шаг, потолок/ДРР). Раз в WB_STEP_DAYS дней поднял → ждём реакции → снова шаг.
    • expensive (ДРР≥потолка): вниз к потолку одним разом (перерасход режем сразу) = cpc × (потолок/ДРР).
    • test (тест-фронт): ОДИН шаг +WB_STEP_PCT% от текущей ставки (со дна: пол×1.10=8.03₽), до потолка теста.
    • keep/repriced (0 рекл-заказов, не в тест-фронте) → пол (бюджет-тест на полу).
    • cut/dead/hold — рекомендации нет (их действие «снять», а не менять ставку)."""
    if verdict == "test":
        base = cpc if (cpc and cpc >= WB_BID_FLOOR) else WB_BID_FLOOR      # ставка неизвестна → считаем со дна
        return round(base * (1 + WB_STEP_PCT / 100.0), 2)                  # +10% за шаг, гоним ежедневно
    if cpc and drr and drr > 0:
        if verdict == "grow":
            rec = cpc * min(1 + WB_STEP_PCT / 100.0, WB_DRR_CEIL / drr)   # +10%, но не выше потолка ДРР
            return max(round(rec, 2), WB_BID_FLOOR)
        if verdict == "expensive":
            rec = cpc * (WB_DRR_CEIL / drr)                                # перерасход — вниз к потолку
            return max(round(rec, 2), WB_BID_FLOOR)
    if verdict in ("grow", "keep", "repriced"):
        return WB_BID_FLOOR    # grow без факт.CPC (кликов не было) / keep / repriced → старт с пола, дальше шаг
    return None

def _margin_gated(account, view="all", q="", sort="spend", limit=500):
    period = _decision_period(account)
    if not period:
        return {"rows": [], "summary": {"period": None}, "view": view, "account": account}
    win_from = period  # начало окна активности = decision-месяц − (N−1) мес
    sql = """
      WITH dm AS (  -- метрики решения: за decision-период (последний ПОЛНЫЙ месяц)
        SELECT nm_id, sum(spend) spend, sum(revenue) rev, sum(clicks) clicks, sum(orders) ad_orders,
               max(name) ad_name
        FROM wb_ad_nm WHERE account=%s AND period=%s GROUP BY nm_id),
      cur AS (  -- КАМПАНИЯ для PATCH — из ТЕКУЩЕГО периода (nm мигрируют между РК; июльская РК даёт
                -- 400 "not found in advert"). adverts[0] = активная, самая кликаемая РG сейчас.
        SELECT nm_id,
               array_agg(advert_id ORDER BY (status=9) DESC, clicks DESC, spend DESC, advert_id) adverts,
               bool_or(status=9) any_active
        FROM wb_ad_nm
        WHERE account=%s AND period=(SELECT max(period) FROM wb_ad_nm WHERE account=%s)
        GROUP BY nm_id),
      s3 AS (  -- активность за 3 полных мес + истор. цена продажи (revenue/qty) для ловушки цены
        SELECT article, sum(qty) q3, sum(revenue_buyer) rev3
        FROM margin_by_sku
        WHERE platform='wb' AND account=%s
          AND period_from > (%s::date - make_interval(months => %s)) AND period_from <= %s
        GROUP BY article),
      ml AS (  -- ЖИВАЯ маржа: текущая цена × себест «купим сегодня». Дедуп: в таблице ~14× дублей
        SELECT DISTINCT ON (nm_id) nm_id, margin_own_live, net_live, buy_status,
               our_price, buy_price_live
        FROM mkt_margin_control WHERE account=%s ORDER BY nm_id),
      imt AS (  -- склейка: nm → imtID (последняя карточка)
        SELECT DISTINCT ON (nm_id) nm_id, (payload->>'imtID')::bigint imt_id
        FROM raw_wb_card_content WHERE account=%s ORDER BY nm_id, collected_at DESC),
      imt_sz AS (SELECT imt_id, count(*) n FROM imt GROUP BY imt_id),
      imt_sales AS (  -- сумма продаж склейки за 3 мес (по всем nm склейки)
        SELECT i.imt_id, sum(s3.q3) imt_q
        FROM imt i JOIN s3 ON s3.article = i.nm_id::text GROUP BY i.imt_id),
      jam AS (  -- последний снимок позиции + предыдущий день (LEAD по DESC) для дельты день-к-дню
        SELECT nm_id, period_start::text jam_date, avg_position, open_card, jam_orders, visibility,
               pos_prev, prev_date::text prev_date
        FROM (
          SELECT nm_id, period_start, avg_position, open_card, orders jam_orders, visibility,
                 lead(avg_position) OVER w pos_prev, lead(period_start) OVER w prev_date,
                 row_number() OVER w rn
          FROM wb_search_report WHERE account=%s
          WINDOW w AS (PARTITION BY nm_id ORDER BY period_start DESC)
        ) t WHERE rn=1),
      ov AS (SELECT nm_id, cpc, source FROM wb_bid_override WHERE account=%s),
      dly AS (  -- ДНЕВНЫЕ рекл. показы per-nm: посл. день + пред. + позиция бустера (реакция на ставку)
        SELECT nm_id, dt, views, booster_pos, row_number() OVER w rn
        FROM (SELECT nm_id, dt, sum(views) views,
                     min(booster_pos) FILTER (WHERE booster_pos IS NOT NULL) booster_pos
              FROM wb_ad_nm_daily WHERE account=%s GROUP BY nm_id, dt) g
        WINDOW w AS (PARTITION BY nm_id ORDER BY dt DESC)),
      dcur  AS (SELECT nm_id, dt::text views_date, views views_daily, booster_pos FROM dly WHERE rn=1),
      dprev AS (SELECT nm_id, views views_prev FROM dly WHERE rn=2)
      SELECT dm.nm_id, c.vendor_code, COALESCE(c.title, dm.ad_name) title, c.subject,
             dm.spend, dm.rev, dm.clicks, dm.ad_orders, cur.adverts, cur.any_active,
             ml.margin_own_live, ml.net_live, ml.buy_status, ml.our_price, ml.buy_price_live,
             s3.q3, s3.rev3,
             i.imt_id, isz.n imt_size, isl.imt_q,
             o.cpc ov_cpc, o.source ov_source,
             j.avg_position, j.open_card, j.jam_orders, j.visibility, j.jam_date, j.pos_prev, j.prev_date,
             dc.views_daily, dc.views_date, dc.booster_pos, dp.views_prev
      FROM dm
      LEFT JOIN cur        ON cur.nm_id = dm.nm_id
      LEFT JOIN s3         ON s3.article = dm.nm_id::text
      LEFT JOIN ml         ON ml.nm_id  = dm.nm_id
      LEFT JOIN imt i      ON i.nm_id   = dm.nm_id
      LEFT JOIN imt_sz isz ON isz.imt_id = i.imt_id
      LEFT JOIN imt_sales isl ON isl.imt_id = i.imt_id
      LEFT JOIN wb_cards c ON c.account=%s AND c.nm_id = dm.nm_id
      LEFT JOIN ov o       ON o.nm_id = dm.nm_id
      LEFT JOIN jam j      ON j.nm_id = dm.nm_id
      LEFT JOIN dcur dc    ON dc.nm_id = dm.nm_id
      LEFT JOIN dprev dp   ON dp.nm_id = dm.nm_id
    """
    params = [account, period,                      # dm
              account, account,                     # cur (WHERE + max-период)
              account, period, WB_ACTIVITY_MONTHS, period,   # s3
              account, account, account, account,   # ml, imt, jam, ov
              account,                              # dly (дневной рекл. трек)
              account]                              # wb_cards
    if q:
        sql += " WHERE (dm.nm_id::text LIKE %s OR c.vendor_code ILIKE %s OR c.title ILIKE %s)"
        like = f"%{q}%"
        params += [like, like, like]
    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        spend = float(r["spend"]) if r["spend"] is not None else 0.0
        rev = float(r["rev"]) if r["rev"] is not None else 0.0
        orders = int(r["ad_orders"] or 0)
        clicks = int(r["clicks"] or 0)
        qty = int(r["q3"]) if r["q3"] is not None else None   # штук продано за 3 мес
        buy_status = r["buy_status"]
        mp = float(r["margin_own_live"]) if r["margin_own_live"] is not None else None
        if buy_status in ("no_price", "unmapped"):
            mp = None   # себест «купим сегодня» не найдена → живую маржу не судим (⚪ маржа ?)
        sold = qty is not None and qty > 0
        our_price = float(r["our_price"]) if r["our_price"] is not None else None
        # истор. цена продажи (наша до СПП) за окно vs текущая живая → дрейф цены
        hist_price = (float(r["rev3"]) / qty) if (sold and r["rev3"] is not None) else None
        drift = round((our_price / hist_price - 1) * 100, 0) if (hist_price and our_price) else None
        repriced = (drift is not None and drift >= WB_REPRICE_DRIFT
                    and (qty or 0) >= WB_REPRICE_MIN_QTY)  # тонкий базис q3<5 → не флагуем (шум цены)
        # склейка
        imt_id = r["imt_id"]
        imt_size = int(r["imt_size"]) if r["imt_size"] is not None else None
        imt_q = int(r["imt_q"]) if r["imt_q"] is not None else 0
        sib_q = imt_q - (qty or 0)  # продажи СОСЕДЕЙ по склейке за 3 мес (не считая себя)
        drr = round(spend / rev * 100, 1) if rev > 0 else None
        cpo = round(spend / orders, 2) if orders > 0 else None
        unit_margin = round(float(r["net_live"]), 2) if r["net_live"] is not None else None
        cpc_dm = round(spend / clicks, 2) if clicks > 0 else None
        cur_cpc = float(r["ov_cpc"]) if r["ov_cpc"] is not None else cpc_dm
        # Реальная НАЗНАЧЕННАЯ ставка = только наш override (прошлый PATCH). Реконструкция расход/клики —
        # это цена клика в аукционе, НЕ ставка; по 1-2 кликам чистый шум → для тест-фронта база = пол.
        assigned_cpc = float(r["ov_cpc"]) if r["ov_cpc"] is not None else None
        # тест-фронт: продавался, в активной кампании, реклама ещё не зацепилась (кликов ≤2),
        # НАЗНАЧЕННАЯ ставка ниже потолка теста → гоним +10%/сутки от назначенной (пол, если не писали).
        test_base = assigned_cpc if assigned_cpc is not None else WB_BID_FLOOR
        on_ramp = test_base < WB_TEST_CAP
        test_ready = bool(r["any_active"]) and bool(sold) and (clicks or 0) <= 2 and on_ramp
        vkey, vlabel = _verdict(sold, mp, drr, spend, orders, repriced, test_ready)
        rec_cpc = _rec_cpc(vkey, assigned_cpc if vkey == "test" else cur_cpc, drr)
        out.append({
            "nm_id": r["nm_id"], "vendor_code": r["vendor_code"], "title": r["title"],
            "subject": r["subject"], "adverts": r["adverts"] or [], "any_active": r["any_active"],
            "spend": round(spend, 2), "rev": round(rev, 2), "clicks": clicks, "ad_orders": orders,
            "margin_pct": mp, "unit_margin": unit_margin, "qty": qty,
            "buy_status": buy_status,
            "our_price_live": round(our_price, 2) if our_price is not None else None,
            "buy_price_live": round(float(r["buy_price_live"]), 2) if r["buy_price_live"] is not None else None,
            "hist_price": round(hist_price, 2) if hist_price is not None else None,
            "price_drift": drift, "repriced": repriced,
            "imt_id": imt_id, "imt_size": imt_size, "sib_q": sib_q,
            "drr": drr, "cpo": cpo, "cpc": cur_cpc, "cpc_source": (r["ov_source"] or ("reconstructed" if cpc_dm is not None else "none")),
            "rec_cpc": rec_cpc,
            "avg_position": r["avg_position"], "open_card": r["open_card"],
            "jam_orders": r["jam_orders"], "visibility": r["visibility"],
            "jam_date": r["jam_date"], "pos_prev": r["pos_prev"], "prev_date": r["prev_date"],
            "views_daily": int(r["views_daily"]) if r["views_daily"] is not None else None,
            "views_prev": int(r["views_prev"]) if r["views_prev"] is not None else None,
            "views_date": r["views_date"],
            "booster_pos": float(r["booster_pos"]) if r["booster_pos"] is not None else None,
            "verdict": vkey, "verdict_label": vlabel,
        })
    # когорты + деньги-в-игре считаем по всему набору (до фильтра вида)
    coh = {"grow": 0, "test": 0, "keep": 0, "repriced": 0, "cut": 0, "dead": 0, "expensive": 0, "hold": 0}
    coh_spend = {"grow": 0.0, "test": 0.0, "keep": 0.0, "repriced": 0.0, "cut": 0.0, "dead": 0.0, "expensive": 0.0, "hold": 0.0}
    for x in out:
        coh[x["verdict"]] += 1
        coh_spend[x["verdict"]] += x["spend"] or 0
    total = len(out)
    total_spend = round(sum(x["spend"] or 0 for x in out), 2)
    total_rev = round(sum(x["rev"] or 0 for x in out), 2)
    buy_stale = sum(1 for x in out if x.get("buy_status") == "stale")
    buy_nopx = sum(1 for x in out if x.get("buy_status") in ("no_price", "unmapped"))
    if view in coh:
        out = [x for x in out if x["verdict"] == view]
    # деньги-в-игре: снимать/чинить — по расходу вниз; растить — по ad-выручке вниз
    keymap = {"spend": lambda x: -(x["spend"] or 0), "revenue": lambda x: -(x["rev"] or 0),
              "margin": lambda x: (x["margin_pct"] is None, -(x["margin_pct"] or 0)),
              "drr": lambda x: (x["drr"] is None, -(x["drr"] or 0)),
              "orders": lambda x: -(x["ad_orders"] or 0),
              "drift": lambda x: (x["price_drift"] is None, -(x["price_drift"] or 0))}
    out.sort(key=keymap.get(sort, keymap["spend"]))
    summary = {
        "period": period, "total": total, "total_spend": total_spend, "total_rev": total_rev,
        "drr_total": round(total_spend / total_rev * 100, 1) if total_rev > 0 else None,
        "cohorts": coh, "cohort_spend": {k: round(v, 2) for k, v in coh_spend.items()},
        "margin_gate": WB_MARGIN_GATE, "drr_ceil": WB_DRR_CEIL,
        "margin_source": "live", "buy_stale": buy_stale, "buy_nopx": buy_nopx,
        "activity_months": WB_ACTIVITY_MONTHS, "reprice_drift": WB_REPRICE_DRIFT,
        "live_enabled": WB_BID_LIVE_ENABLED, "floor_cpc": WB_BID_FLOOR,
        "step_pct": WB_STEP_PCT, "step_days": WB_STEP_DAYS,
    }
    return {"period": period, "rows": out[:limit], "summary": summary,
            "sort": sort, "view": view, "account": account}


@app.get("/api/wb-bids")
def wb_bids(account: str = "wb_acc1", view: str = "all", q: str = "", limit: int = 500,
            mode: str = "margin", sort: str = "spend"):
    """mode=margin (по умолчанию) → маржа-гейтед вид: решение по SKU (растить/снять/чистить) за
    последний ПОЛНЫЙ месяц (маржа×ДРР×вердикт). mode=bids → таблица текущих ставок (ручной ввод/dry).
    Картина ставок WB per-nmID. «Текущая» = COALESCE(ручной оверрайд, реконструкция CPC=расход/клики
    из wb_ad_nm за последний период). view: all | floor (на полу ~7.3₽) | no_data (ставку не знаем —
    кандидаты на ручной ввод) | raised (выше пола) | override (ручные/api). Рядом — свежий Джем
    (позиция/показы/заказы) и список кампаний, где SKU крутится."""
    if mode == "margin":
        return _margin_gated(account, view=view, q=q, sort=sort, limit=limit)
    # база: nm из рекламы (последние 2 периода) ∪ nm с ручным оверрайдом.
    # recon_cpc = факт.CPC (расход/клики) из САМОГО СВЕЖЕГО периода, где были клики (ставка держится
    # per-nm, поэтому клики прошлого месяца — годная оценка текущей ставки, если в этом ещё не кликали).
    # Кампании/активность — из последнего периода (что крутится сейчас).
    sql = """
      WITH periods AS (
        SELECT DISTINCT period FROM wb_ad_nm WHERE account=%s ORDER BY period DESC LIMIT 2),
      p_all AS (
        SELECT n.nm_id, n.period,
               sum(n.spend) spend, sum(n.clicks) clicks, sum(n.orders) ad_orders,
               array_agg(DISTINCT n.advert_id ORDER BY n.advert_id) adverts,
               max(n.name) ad_name, bool_or(n.status=9) any_active,
               max(n.period) OVER () maxp
        FROM wb_ad_nm n WHERE n.account=%s AND n.period IN (SELECT period FROM periods)
        GROUP BY n.nm_id, n.period),
      recon_cpc AS (
        SELECT DISTINCT ON (nm_id) nm_id,
               CASE WHEN clicks>0 THEN round(spend/clicks,2) END recon_cpc,
               clicks recon_clicks, period::text recon_period
        FROM p_all ORDER BY nm_id, (clicks>0) DESC, period DESC),
      recon AS (
        SELECT l.nm_id, rc.recon_cpc, rc.recon_clicks, rc.recon_period,
               l.spend, l.clicks, l.ad_orders, l.adverts, l.ad_name, l.any_active
        FROM p_all l LEFT JOIN recon_cpc rc ON rc.nm_id=l.nm_id
        WHERE l.period = l.maxp),
      jam AS (
        SELECT DISTINCT ON (nm_id) nm_id, period_start::text jam_date, avg_position,
               open_card, orders jam_orders, visibility, is_advertised
        FROM wb_search_report WHERE account=%s ORDER BY nm_id, period_start DESC),
      universe AS (
        SELECT nm_id FROM recon
        UNION SELECT nm_id FROM wb_bid_override WHERE account=%s)
      SELECT u.nm_id, c.vendor_code, COALESCE(c.title, r.ad_name) title, c.subject,
             o.cpc ov_cpc, o.source ov_source, o.updated_at::text ov_at, o.note ov_note,
             r.recon_cpc, r.recon_clicks, r.recon_period, r.clicks, r.spend, r.ad_orders,
             r.adverts, r.any_active,
             j.jam_date, j.avg_position, j.open_card, j.jam_orders, j.visibility
      FROM universe u
      LEFT JOIN recon r            ON r.nm_id = u.nm_id
      LEFT JOIN wb_bid_override o  ON o.account=%s AND o.nm_id = u.nm_id
      LEFT JOIN wb_cards c         ON c.account=%s AND c.nm_id = u.nm_id
      LEFT JOIN jam j              ON j.nm_id = u.nm_id
    """
    params = [account, account, account, account, account, account]
    if q:
        sql += " WHERE (u.nm_id::text LIKE %s OR c.vendor_code ILIKE %s OR c.title ILIKE %s)"
        like = f"%{q}%"
        params += [like, like, like]
    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        cur = r["ov_cpc"] if r["ov_cpc"] is not None else r["recon_cpc"]
        src = r["ov_source"] if r["ov_cpc"] is not None else ("reconstructed" if r["recon_cpc"] is not None else "none")
        on_floor = cur is not None and float(cur) <= WB_BID_FLOOR + 0.2
        r2 = dict(r)
        r2["cpc"] = float(cur) if cur is not None else None
        r2["cpc_source"] = src
        r2["on_floor"] = on_floor
        r2["adverts"] = r["adverts"] or []
        out.append(r2)
    # фильтры-виды
    def keep(x):
        if view == "floor":    return x["cpc_source"] != "none" and x["on_floor"]
        if view == "raised":   return x["cpc_source"] != "none" and not x["on_floor"]
        if view == "no_data":  return x["cpc_source"] == "none"
        if view == "override": return x["cpc_source"] in ("manual", "api_set")
        return True
    out = [x for x in out if keep(x)]
    out.sort(key=lambda x: (x["avg_position"] is None, x["avg_position"] or 1e9))
    # подписи периодов: Джем — скользящее окно 7 дней (period_start..+6); реклама — период реконструкции
    jw = db.query("SELECT max(period_start)::text s FROM wb_search_report WHERE account=%s", (account,))
    jam_start = jw[0]["s"] if jw and jw[0]["s"] else None
    jam_end = (datetime.date.fromisoformat(jam_start) + datetime.timedelta(days=6)).isoformat() if jam_start else None
    ap = db.query("SELECT max(period)::text p FROM wb_ad_nm WHERE account=%s", (account,))
    ad_period = ap[0]["p"] if ap and ap[0]["p"] else None
    summary = {
        "total": len(rows),
        "floor": sum(1 for x in out if False) or sum(1 for r in rows
                     if (r["ov_cpc"] or r["recon_cpc"]) is not None
                     and float(r["ov_cpc"] or r["recon_cpc"]) <= WB_BID_FLOOR + 0.2),
        "no_data": sum(1 for r in rows if r["ov_cpc"] is None and r["recon_cpc"] is None),
        "manual": sum(1 for r in rows if r["ov_source"] == "manual"),
        "floor_cpc": WB_BID_FLOOR,
        "live_enabled": WB_BID_LIVE_ENABLED,
        "jam_start": jam_start, "jam_end": jam_end, "ad_period": ad_period,
    }
    return {"rows": out[:limit], "summary": summary, "view": view, "account": account}


@app.post("/api/wb-bids/manual")
def wb_bid_manual(payload: dict = Body(...)):
    """Ручной ввод ТЕКУЩЕЙ ставки per-nmID (заполнить пробел, где кликов не было → реконструкции нет).
    Пишет оверрайд (source=manual) и строку в журнал с baseline Джема. Это фиксация факта, не запись в WB."""
    account = payload.get("account", "wb_acc1")
    nm_id = int(payload["nm_id"])
    cpc = round(float(payload["cpc"]), 2)
    note = (payload.get("note") or "").strip() or None
    advert_id = payload.get("advert_id")
    prev = db.query("SELECT cpc, source FROM wb_bid_override WHERE account=%s AND nm_id=%s", (account, nm_id))
    old_cpc = float(prev[0]["cpc"]) if prev else None
    old_src = prev[0]["source"] if prev else None
    db.upsert("wb_bid_override",
              [{"account": account, "nm_id": nm_id, "cpc": cpc, "source": "manual",
                "advert_id": advert_id, "note": note, "author": "dashboard",
                "updated_at": datetime.datetime.now()}],
              conflict_cols=["account", "nm_id"],
              update_cols=["cpc", "source", "advert_id", "note", "updated_at"])
    b = _jam_baseline(account, nm_id)
    _bid_log({"account": account, "nm_id": nm_id, "advert_id": advert_id, "action": "manual_set",
              "applied": False, "old_cpc": old_cpc, "new_cpc": cpc,
              "old_source": old_src or "none", "author": "dashboard", "note": note,
              "base_date": b["base_date"], "pos_before": b["avg_position"],
              "open_before": b["open_card"], "orders_before": b["orders"]})
    return {"ok": True, "nm_id": nm_id, "cpc": cpc, "source": "manual"}


@app.post("/api/wb-bids/change")
def wb_bid_change(payload: dict = Body(...)):
    """Смена ставки per-nmID. dry_run=true (по умолчанию) — только собрать и показать запрос, что
    ушёл бы в WB, + записать в журнал (applied=false), реальную ставку НЕ трогать. dry_run=false —
    ЖИВАЯ запись в WB: ВЫКЛЮЧЕНА (WB_BID_LIVE_ENABLED=False) до подтверждения контракта и команды Сергея."""
    account = payload.get("account", "wb_acc1")
    nm_id = int(payload["nm_id"])
    new_cpc = round(float(payload["new_cpc"]), 2)
    advert_id = payload.get("advert_id")
    dry_run = bool(payload.get("dry_run", True))
    note = (payload.get("note") or "").strip() or None
    # текущая (оверрайд → реконструкция)
    ov = db.query("SELECT cpc, source FROM wb_bid_override WHERE account=%s AND nm_id=%s", (account, nm_id))
    if ov:
        old_cpc, old_src = float(ov[0]["cpc"]), ov[0]["source"]
    else:
        rc = db.query("""SELECT CASE WHEN sum(clicks)>0 THEN round(sum(spend)/sum(clicks),2) END c
                         FROM wb_ad_nm WHERE account=%s AND nm_id=%s
                         AND period=(SELECT max(period) FROM wb_ad_nm WHERE account=%s)""",
                      (account, nm_id, account))
        old_cpc = float(rc[0]["c"]) if rc and rc[0]["c"] is not None else None
        old_src = "reconstructed" if old_cpc is not None else "none"
    if new_cpc < WB_BID_FLOOR:
        return {"ok": False, "error": f"Ставка {new_cpc}₽ ниже пола WB {WB_BID_FLOOR}₽ (поиск). "
                f"Минимум ставит POST /bids/min."}
    # запрос к WB по ПОДТВЕРЖДЁННОМУ контракту (для dry-run — что ушло бы живьём через /apply)
    req = {"_endpoint": "PATCH /api/advert/v1/bids",
           "bids": [{"advert_id": advert_id,
                     "nm_bids": [{"nm_id": nm_id, "bid_kopecks": round(new_cpc * 100),
                                  "placement": "search"}]}]}
    b = _jam_baseline(account, nm_id)
    base = {"base_date": b["base_date"], "pos_before": b["avg_position"],
            "open_before": b["open_card"], "orders_before": b["orders"]}
    if dry_run:
        _bid_log({"account": account, "nm_id": nm_id, "advert_id": advert_id, "action": "dry_run",
                  "applied": False, "old_cpc": old_cpc, "new_cpc": new_cpc, "old_source": old_src,
                  "author": "dashboard", "note": note, "req_json": json.dumps(req), **base})
        return {"ok": True, "dry_run": True, "old_cpc": old_cpc, "old_source": old_src,
                "new_cpc": new_cpc, "would_send": req,
                "msg": "Dry-run: записан в журнал, ставка в WB НЕ изменена."}
    # живой путь вынесен в отдельный эндпоинт /api/wb-bids/apply (UI шлёт «применить» туда)
    return {"ok": False, "error": "Для живой записи используйте POST /api/wb-bids/apply."}


def _wb_patch_bid(token, advert_id, nm_id, new_cpc):
    """Живой PATCH ставки в WB по подтверждённому контракту. Возвращает (status, resp_json_or_text)."""
    body = {"bids": [{"advert_id": int(advert_id),
                      "nm_bids": [{"nm_id": int(nm_id), "bid_kopecks": round(float(new_cpc) * 100),
                                   "placement": "search"}]}]}
    r = requests.patch(WB_ADS_HOST + "/api/advert/v1/bids", json=body,
                       headers={"Authorization": token, "Content-Type": "application/json"}, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_text": r.text[:500]}


def _wb_patch_bids_group(token, advert_id, nm_cpcs):
    """Групповой PATCH: одна кампания, МНОГО nmID за запрос (nm_bids — массив). nm_cpcs=[(nm,cpc),...].
    Возвращает (status, resp). Используется массовым применением, чтобы не слать N HTTP по одной ставке."""
    body = {"bids": [{"advert_id": int(advert_id),
                      "nm_bids": [{"nm_id": int(nm), "bid_kopecks": round(float(cpc) * 100),
                                   "placement": "search"} for nm, cpc in nm_cpcs]}]}
    r = requests.patch(WB_ADS_HOST + "/api/advert/v1/bids", json=body,
                       headers={"Authorization": token, "Content-Type": "application/json"}, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_text": r.text[:500]}


def _old_cpc_src(account, nm_id):
    """Текущая ставка per-nm для журнала: оверрайд → иначе реконструкция факт.CPC (расход/клики) за
    последний период. Возвращает (old_cpc|None, source)."""
    ov = db.query("SELECT cpc, source FROM wb_bid_override WHERE account=%s AND nm_id=%s", (account, nm_id))
    if ov:
        return float(ov[0]["cpc"]), ov[0]["source"]
    rc = db.query("""SELECT CASE WHEN sum(clicks)>0 THEN round(sum(spend)/sum(clicks),2) END c
                     FROM wb_ad_nm WHERE account=%s AND nm_id=%s
                     AND period=(SELECT max(period) FROM wb_ad_nm WHERE account=%s)""",
                  (account, nm_id, account))
    c = float(rc[0]["c"]) if rc and rc[0]["c"] is not None else None
    return c, ("reconstructed" if c is not None else "none")


@app.post("/api/wb-bids/apply")
def wb_bid_apply(payload: dict = Body(...)):
    """ЖИВАЯ смена ставки per-nmID в WB (PATCH /api/advert/v1/bids). Требует advert_id (ставка задаётся
    в конкретной кампании). Пол WB (7.3₽) — гейт. При успехе (200): апсерт оверрайда source=api_set +
    журнал applied=true с req/resp и baseline Джема (для оценки эффекта через неделю). Если живой путь
    выключен/нет токена — пишет намерение в журнал (applied=false) и возвращает blocked (деградация)."""
    account = payload.get("account", "wb_acc1")
    nm_id = int(payload["nm_id"])
    new_cpc = round(float(payload["new_cpc"]), 2)
    advert_id = payload.get("advert_id")
    note = (payload.get("note") or "").strip() or None
    if new_cpc < WB_BID_FLOOR:
        return {"ok": False, "error": f"Ставка {new_cpc}₽ ниже пола WB {WB_BID_FLOOR}₽ (поиск)."}
    if not advert_id:
        return {"ok": False, "error": "Нужен advert_id: ставка задаётся в конкретной кампании."}
    # текущая (оверрайд → реконструкция) — для old_cpc в журнале
    old_cpc, old_src = _old_cpc_src(account, nm_id)
    b = _jam_baseline(account, nm_id)
    base = {"base_date": b["base_date"], "pos_before": b["avg_position"],
            "open_before": b["open_card"], "orders_before": b["orders"]}
    req = {"_endpoint": "PATCH /api/advert/v1/bids",
           "bids": [{"advert_id": advert_id, "nm_bids": [{"nm_id": nm_id,
                     "bid_kopecks": round(new_cpc * 100), "placement": "search"}]}]}
    token = os.getenv(WB_ADS_TOKEN_ENV.get(account, ""), "")
    if not (WB_BID_LIVE_ENABLED and token):
        _bid_log({"account": account, "nm_id": nm_id, "advert_id": advert_id, "action": "api_set",
                  "applied": False, "old_cpc": old_cpc, "new_cpc": new_cpc, "old_source": old_src,
                  "author": "dashboard", "note": note, "req_json": json.dumps(req, ensure_ascii=False), **base})
        return {"ok": False, "blocked": True,
                "error": "Живая запись выключена или нет токена — намерение записано в журнал (applied=false)."}
    status, resp = _wb_patch_bid(token, advert_id, nm_id, new_cpc)
    applied = status == 200
    _bid_log({"account": account, "nm_id": nm_id, "advert_id": advert_id, "action": "api_set",
              "applied": applied, "old_cpc": old_cpc, "new_cpc": new_cpc, "old_source": old_src,
              "author": "dashboard", "note": note,
              "req_json": json.dumps(req, ensure_ascii=False),
              "resp_status": status, "resp_json": json.dumps(resp, ensure_ascii=False), **base})
    if applied:
        db.upsert("wb_bid_override",
                  [{"account": account, "nm_id": nm_id, "cpc": new_cpc, "source": "api_set",
                    "advert_id": advert_id, "note": note, "author": "dashboard",
                    "updated_at": datetime.datetime.now()}],
                  conflict_cols=["account", "nm_id"],
                  update_cols=["cpc", "source", "advert_id", "note", "updated_at"])
        return {"ok": True, "applied": True, "old_cpc": old_cpc, "new_cpc": new_cpc,
                "resp_status": status, "msg": f"Ставка nm {nm_id} → {new_cpc}₽ отправлена в WB (200)."}
    return {"ok": False, "applied": False, "resp_status": status, "resp": resp,
            "error": f"WB отклонил смену ставки (HTTP {status}). Записано в журнал."}


@app.post("/api/wb-bids/remove")
def wb_bid_remove(payload: dict = Body(...)):
    """Снять nmID с рекламы. API снятия у WB НЕ найдено (namespace управления составом за антиботом,
    /bids только PATCH) → ОЧЕРЕДЬ: пишем намерение в журнал (action='remove', applied=false), фактическое
    исключение nmID делается вручную в ЛК ВБ. Ставку 0 WB не принимает (пол 7.3₽), поэтому только снятие."""
    account = payload.get("account", "wb_acc1")
    nm_id = int(payload["nm_id"])
    advert_id = payload.get("advert_id")
    note = (payload.get("note") or "").strip() or None
    b = _jam_baseline(account, nm_id)
    req = {"_note": "API снятия nmID у WB не найдено — ручное исключение в ЛК ВБ",
           "advert_id": advert_id, "nm_id": nm_id}
    _bid_log({"account": account, "nm_id": nm_id, "advert_id": advert_id, "action": "remove",
              "applied": False, "old_source": "queue", "author": "dashboard", "note": note,
              "req_json": json.dumps(req, ensure_ascii=False),
              "base_date": b["base_date"], "pos_before": b["avg_position"],
              "open_before": b["open_card"], "orders_before": b["orders"]})
    return {"ok": True, "queued": True,
            "msg": f"Намерение снять nm {nm_id} записано в журнал. Исключите его вручную в ЛК ВБ "
                   f"(API снятия у WB нет)."}


WB_BULK_MAX = 500  # предохранитель на размер одной пачки


@app.post("/api/wb-bids/apply-bulk")
def wb_bid_apply_bulk(payload: dict = Body(...)):
    """МАССОВОЕ применение ставок: список items=[{nm_id, advert_id, new_cpc}]. Группируем по кампании
    (один PATCH на кампанию, nm_bids — массив) → живой батч в WB. Гейт пола на каждую; строки без
    advert_id или ниже пола — в skipped (не шлём). По каждой применённой — журнал (action=api_set,
    applied) + оверрайд source=api_set + baseline Джема. Один вызов = одна санкционированная пачка.
    Живой путь выключен/нет токена → всё пишется как намерение (applied=false) и возвращается blocked."""
    account = payload.get("account", "wb_acc1")
    items = payload.get("items") or []
    if not items:
        return {"ok": False, "error": "Пустой список items."}
    if len(items) > WB_BULK_MAX:
        return {"ok": False, "error": f"Слишком большая пачка ({len(items)} > {WB_BULK_MAX}). Разбейте."}
    token = os.getenv(WB_ADS_TOKEN_ENV.get(account, ""), "")
    live = bool(WB_BID_LIVE_ENABLED and token)
    # валидация + группировка по кампании
    groups, skipped = {}, []
    for it in items:
        try:
            nm = int(it["nm_id"]); cpc = round(float(it["new_cpc"]), 2)
        except (KeyError, TypeError, ValueError):
            skipped.append({"nm_id": it.get("nm_id"), "reason": "плохие данные строки"}); continue
        adv = it.get("advert_id")
        if not adv:
            skipped.append({"nm_id": nm, "reason": "нет активной кампании (advert_id)"}); continue
        if cpc < WB_BID_FLOOR:
            skipped.append({"nm_id": nm, "reason": f"ниже пола WB {WB_BID_FLOOR}₽"}); continue
        groups.setdefault(int(adv), []).append((nm, cpc))
    applied_cnt, err_cnt, results = 0, 0, []
    for adv, lst in groups.items():
        # групповой PATCH быстрым путём; но у WB пачка АТОМАРНА — один nm «not found in advert» валит
        # всю группу. При не-200 и >1 позиции добиваем по одному, чтобы валидные соседи применились,
        # а сбойный получил СВОЙ ответ (иначе в журнал соседям писался чужой resp).
        per_status, per_resp = {}, {}
        if not live:
            for nm, _ in lst:
                per_status[nm] = None; per_resp[nm] = None
        else:
            gstatus, gresp = _wb_patch_bids_group(token, adv, lst)
            if gstatus == 200 or len(lst) == 1:
                for nm, _ in lst:
                    per_status[nm] = gstatus; per_resp[nm] = gresp
            else:
                for nm, cpc in lst:
                    st, rs = _wb_patch_bid(token, adv, nm, cpc)
                    per_status[nm] = st; per_resp[nm] = rs
        for nm, cpc in lst:
            status = per_status[nm]
            resp = per_resp[nm]
            ok = bool(live and status == 200)
            resp_s = json.dumps(resp, ensure_ascii=False) if resp is not None else None
            old_cpc, old_src = _old_cpc_src(account, nm)
            b = _jam_baseline(account, nm)
            req = {"_endpoint": "PATCH /api/advert/v1/bids", "_bulk": True,
                   "bids": [{"advert_id": adv, "nm_bids": [{"nm_id": nm,
                             "bid_kopecks": round(cpc * 100), "placement": "search"}]}]}
            _bid_log({"account": account, "nm_id": nm, "advert_id": adv, "action": "api_set",
                      "applied": ok, "old_cpc": old_cpc, "new_cpc": cpc, "old_source": old_src,
                      "author": "dashboard-bulk", "note": None,
                      "req_json": json.dumps(req, ensure_ascii=False),
                      "resp_status": status, "resp_json": resp_s,
                      "base_date": b["base_date"], "pos_before": b["avg_position"],
                      "open_before": b["open_card"], "orders_before": b["orders"]})
            if ok:
                db.upsert("wb_bid_override",
                          [{"account": account, "nm_id": nm, "cpc": cpc, "source": "api_set",
                            "advert_id": adv, "note": None, "author": "dashboard-bulk",
                            "updated_at": datetime.datetime.now()}],
                          conflict_cols=["account", "nm_id"],
                          update_cols=["cpc", "source", "advert_id", "note", "updated_at"])
                applied_cnt += 1
            else:
                err_cnt += 1
            results.append({"nm_id": nm, "advert_id": adv, "new_cpc": cpc,
                            "applied": ok, "resp_status": status})
    msg = (f"Применено {applied_cnt}, ошибок {err_cnt}, пропущено {len(skipped)}." if live
           else f"Живая запись выключена — {applied_cnt + err_cnt} намерений в журнал (applied=false).")
    return {"ok": True, "blocked": (not live), "live": live,
            "applied_count": applied_cnt, "error_count": err_cnt,
            "skipped": skipped, "results": results, "msg": msg}


@app.get("/api/wb-bids/log")
def wb_bids_log(account: str = "wb_acc1", nm_id: int = 0, limit: int = 200):
    """Журнал смен ставок + последствия. Дельта «до/после» (позиция/показы/заказы) считается из
    СВЕЖЕГО Джема относительно baseline на момент смены. up/down по позиции: меньше позиция = лучше."""
    where = ["l.account=%s"]
    params = [account]
    if nm_id:
        where.append("l.nm_id=%s")
        params.append(nm_id)
    rows = db.query(f"""
      WITH jam AS (
        SELECT DISTINCT ON (nm_id) nm_id, period_start::text jam_date, avg_position, open_card, orders
        FROM wb_search_report WHERE account=%s ORDER BY nm_id, period_start DESC)
      SELECT l.id, l.ts::text ts, (now()::date - l.ts::date) age_days,
             l.nm_id, l.advert_id, l.action, l.applied,
             l.old_cpc, l.new_cpc, l.old_source, l.note,
             COALESCE(c.title, l.nm_id::text) title, c.vendor_code,
             l.base_date, l.pos_before, l.open_before, l.orders_before,
             j.jam_date pos_after_date, j.avg_position pos_after, j.open_card open_after, j.orders orders_after
      FROM wb_bid_log l
      LEFT JOIN wb_cards c ON c.account=l.account AND c.nm_id=l.nm_id
      LEFT JOIN jam j ON j.nm_id=l.nm_id
      WHERE {' AND '.join(where)}
      ORDER BY l.ts DESC LIMIT %s
    """, (account, *params, limit))
    out = []
    for r in rows:
        d = dict(r)
        pb, pa = r["pos_before"], r["pos_after"]
        pos_delta = (float(pa) - float(pb)) if pb is not None and pa is not None else None
        d["pos_delta"] = pos_delta
        ob, oa = r["orders_before"], r["orders_after"]
        orders_delta = (oa - ob) if ob is not None and oa is not None else None
        d["orders_delta"] = orders_delta
        # оценка после шага: только для ПРИМЕНЁННЫХ смен ставки (не dry/remove), возрастом ≥WB_STEP_DAYS.
        # Шагаем +10% раз в 2 дня и смотрим реакцию — на 2-й день строка зовёт «пора оценить».
        # Сигнал берём из свежего Джема (позиция ↓ = лучше; заказы ↑). Расход/ДРР посуточно per-nm
        # у нас нет (wb_ad_nm помесячно) → честная оценка Джем-центрична.
        age = int(r["age_days"]) if r["age_days"] is not None else None
        measurable = bool(r["applied"]) and r["action"] in ("api_set", "manual_set")
        d["due_review"] = bool(measurable and age is not None and age >= WB_STEP_DAYS)
        d["eval_verdict"] = None
        if d["due_review"]:
            raised = (r["new_cpc"] is not None and r["old_cpc"] is not None
                      and float(r["new_cpc"]) > float(r["old_cpc"]))
            improved = (pos_delta is not None and pos_delta <= -1) or (orders_delta is not None and orders_delta > 0)
            if improved:
                d["eval_verdict"] = "сработало"
            elif raised:
                d["eval_verdict"] = "дороже впустую"
            else:
                d["eval_verdict"] = "нет эффекта"
        out.append(d)
    return {"rows": out, "account": account}
