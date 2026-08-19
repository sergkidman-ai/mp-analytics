#!/usr/bin/env python3
# поток: mkt (mkt-ozon)
"""ozon_weekly_bids.py — недельный цикл управления ставками Ozon с журналом решений.

Политика (задана Сергеем 17.08.2026, взамен ежедневного разгона «+10 % до потолка»):
раз в неделю смотрим ПРОШЛУЮ ПОЛНУЮ неделю и двигаем ставку на один шаг ±10 %,
никуда не разгоняясь и ни в какой потолок не упираясь.

  🟢 GREEN     +10 %  — заказы есть, реклама окупается (ДРР ≤ 10 %);
  🟡 YELLOW    держим — заказы есть, но реклама уже подъедает (ДРР 10–15 %);
  🔴 RED       −10 %  — заказы есть, но реклама ест (ДРР > 15 %);
  🟤 BORDEAUX  в пол  — кликов достаточно, заказов нет. НЕ снимаем: ставка в полу
                        продолжает давать показы, а показы работают на всю выдачу
                        (эффект ореола — снятие волны 1 09.08 стоило нам половины
                        платных показов acc1). Снятие — только после REMOVE_WEEKS
                        недель подряд в полу без единого заказа и по прямой команде;
  ➕ ENTER     завести — не в рекламе, но прошлую неделю отработал хорошо сам;
  ⏸ WATCH     нет данных — кликов слишком мало, чтобы вообще судить.

Главное отличие от разгона: решение принимается ПО ФАКТУ прошедшей недели, а не по тому,
что «есть запас по марже». Потолок здесь — только страховка (выше него не поднимаем),
а не цель.

ЖУРНАЛ. Каждое решение пишется в mkt_ozon_bid_journal: тир, действие, ставка до/после,
причина словами и снимок метрик недели (m_before). Через неделю `review` дописывает
метрики после (m_after) и вердикт: оправдалось решение или нет. Это и есть память —
следующий `plan` читает прошлые вердикты и не наступает второй раз на те же грабли.

Команды:
  plan   [--week YYYY-MM-DD]  — разобрать неделю, разложить по тирам, записать в журнал.
                                НИЧЕГО не отправляет на площадку.
  review [--week YYYY-MM-DD]  — оценить решения, принятые неделей раньше.
  apply  --week ... --apply   — отправить ставки в Ozon. ТОЛЬКО по прямой команде Сергея.

Горизонты (разные, это важно):
  реклама  — mkt_ozon_ads_sku_daily, посуточно (с 27.07.2026);
  органика — ozon_search_product / ozon_search_query, недели, лаг Ozon ~2 дня
             (на момент решения последняя закрытая неделя обычно на неделю старше);
  продажи  — raw_ozon_posting; экономика — mkt_ozon_margin_control (последний снимок).

Маржа, потолки и KPI в решении НЕ участвуют (убраны 19.08.2026 по команде Сергея:
«сделай базово, не заморачивайся»). Единственный критерий — факт прошедшей недели:
есть заказы и какой ДРР. margin_safe и ceiling остаются в отчёте справочными колонками.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db

OUT = '/opt/mp-analytics/docs/reports'
PPO_RATE = 0.05           # оплата за заказ, оба аккаунта с 09-10.08
MARGIN_HAIRCUT = 10.6     # п.п., поправка к margin_own_live (бриф п.34)
KPI_MARGIN = 17.0         # справочно, в решении не участвует
STEP_PCT = 10.0           # шаг недели, вверх и вниз
DRR_GOOD = 10.0           # ДРР не выше — растим
DRR_LIMIT = 15.0          # выше — снижаем
MIN_CLICKS = 8            # меньше кликов за неделю — судить не о чем
MIN_SPEND_JUDGE = 50.0    # ...либо расход меньше этого
BID_FLOOR = 6.0           # «в пол»: минимальная рабочая ставка
HEADROOM_MIN = 10.0       # справочно, в решении не участвует
REMOVE_WEEKS = 4          # столько недель подряд в полу без заказов — только тогда снятие
CR = {1: 0.06, 2: 0.05, 3: 0.04, 4: 0.03, 5: 0.02}   # клик→заказ по ценовым бакетам

f2 = lambda x: float(x or 0)
r1 = lambda x: round(x, 1) if x is not None else None


def price_bucket(p):
    p = f2(p)
    return 1 if p < 1000 else 2 if p < 2000 else 3 if p < 5000 else 4 if p < 10000 else 5


def ensure_journal():
    db.execute("""
    CREATE TABLE IF NOT EXISTS mkt_ozon_bid_journal (
        id            bigserial PRIMARY KEY,
        decided_on    date        NOT NULL,     -- дата решения
        week_start    date        NOT NULL,     -- неделя, по которой судили
        account       text        NOT NULL,
        campaign_id   text        NOT NULL,
        sku           text        NOT NULL,
        offer_id      text,
        name          text,
        tier          text        NOT NULL,     -- green/yellow/red/bordeaux/enter/watch
        action        text        NOT NULL,     -- up10/hold/down10/floor/enter/exit
        bid_before    numeric,
        bid_after     numeric,
        reason        text        NOT NULL,     -- почему, словами
        m_before      jsonb,                    -- метрики недели, на которых решали
        applied       boolean     DEFAULT false,
        applied_at    timestamptz,
        api_response  text,
        review_on     date,                     -- когда оценили
        m_after       jsonb,                    -- метрики следующей недели
        outcome       text,                     -- justified/not_justified/no_signal
        outcome_note  text,
        created_at    timestamptz DEFAULT now(),
        UNIQUE (week_start, account, campaign_id, sku))""")
    db.execute("""CREATE INDEX IF NOT EXISTS mkt_ozon_bid_journal_sku_idx
                    ON mkt_ozon_bid_journal (account, sku, week_start)""")


def last_full_week(acc):
    """Понедельник последней недели, полностью закрытой в рекламной витрине."""
    r = db.query("SELECT max(stat_date) d FROM mkt_ozon_ads_sku_daily WHERE account=%s", (acc,))[0]
    last = r['d']
    if not last:
        raise SystemExit('в mkt_ozon_ads_sku_daily нет данных')
    mon = last - dt.timedelta(days=last.weekday())     # понедельник недели last
    return mon if last.weekday() == 6 else mon - dt.timedelta(days=7)


def fetch_week(acc, w0):
    """Срез по (кампания, SKU) за неделю w0 и предыдущую, с органикой и экономикой."""
    w1 = w0 + dt.timedelta(days=6)
    p0 = w0 - dt.timedelta(days=7)
    p1 = w0 - dt.timedelta(days=1)
    # последняя закрытая неделя поиска, не позже конца рассматриваемой недели
    sw = db.query("""SELECT max(period_start) w FROM ozon_search_product
                      WHERE account=%s AND period_end - period_start >= 6 AND period_end <= %s""",
                  (acc, w1 + dt.timedelta(days=3)))[0]['w']

    rows = db.query("""
    WITH cur AS (
        SELECT campaign_id::text cid, sku::text sku, max(offer_id) offer_id,
               sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) orders, sum(orders_money) rev,
               avg(bid) FILTER (WHERE bid > 0) bid_avg,
               count(DISTINCT stat_date) days
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date BETWEEN %(w0)s AND %(w1)s GROUP BY 1,2),
    prv AS (
        SELECT campaign_id::text cid, sku::text sku,
               sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) orders, sum(orders_money) rev,
               avg(bid) FILTER (WHERE bid > 0) bid_avg,
               count(DISTINCT stat_date) days
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date BETWEEN %(p0)s AND %(p1)s GROUP BY 1,2),
    cum AS (   -- вся доступная история рекламы: для длинного хвоста неделя ничего не решает
        SELECT campaign_id::text cid, sku::text sku, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) orders, count(DISTINCT stat_date) days
          FROM mkt_ozon_ads_sku_daily WHERE account=%(a)s GROUP BY 1,2),
    bids AS (
        SELECT campaign_id::text cid, sku::text sku, max(bid) bid,
               max(campaign_title) camp
          FROM ozon_bids
         WHERE account=%(a)s
           AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(a)s)
         GROUP BY 1,2),
    org AS (
        SELECT sku::text sku, sum(unique_search_users) demand, sum(unique_view_users) views,
               sum(gmv) gmv, avg(position) FILTER (WHERE position > 0) pos
          FROM ozon_search_product
         WHERE account=%(a)s AND period_start=%(sw)s GROUP BY 1),
    org_prev AS (
        SELECT sku::text sku, sum(unique_view_users) views, sum(gmv) gmv,
               avg(position) FILTER (WHERE position > 0) pos
          FROM ozon_search_product
         WHERE account=%(a)s AND period_start=%(sw)s - 7 GROUP BY 1),
    ph AS (   -- фразовый слой: спрос, лучшая позиция, сколько фраз в топ-10
        SELECT sku::text sku, sum(unique_search_users) ph_demand,
               min(position) FILTER (WHERE position > 0) ph_best,
               count(*) FILTER (WHERE position > 0 AND position <= 10) ph_top10,
               count(*) ph_n
          FROM ozon_search_query
         WHERE account=%(a)s AND period_start=%(sw)s GROUP BY 1),
    sold AS (
        SELECT pr->>'sku' sku,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at::date BETWEEN %(w0)s AND %(w1)s
                        THEN (pr->>'quantity')::numeric ELSE 0 END) qty_w,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at::date BETWEEN %(w0)s AND %(w1)s
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev_w,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at::date BETWEEN %(p0)s AND %(p1)s
                        THEN (pr->>'quantity')::numeric ELSE 0 END) qty_p,
               max(p.in_process_at) FILTER (WHERE p.status<>'cancelled')::date last_sale
          FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
         WHERE p.account=%(a)s AND p.in_process_at >= %(p0)s GROUP BY 1),
    sold_all AS (   -- продавался ли вообще хоть раз за всю доступную историю
        SELECT pr->>'sku' sku,
               sum(CASE WHEN p.status<>'cancelled' THEN (pr->>'quantity')::numeric ELSE 0 END) qty_all
          FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
         WHERE p.account=%(a)s GROUP BY 1),
    marg AS (
        SELECT sku::text sku, our_price, margin_own_live
          FROM mkt_ozon_margin_control
         WHERE account=%(a)s
           AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control
                               WHERE account=%(a)s)),
    ms AS (    -- остаток по складу МоегоСклада: аккаунты ФБС, ozon_fbo_stock почти пуст
        SELECT article, sum(stock) ms_stock
          FROM supplier_stock
         WHERE captured_at=(SELECT max(captured_at) FROM supplier_stock)
         GROUP BY 1),
    stock AS (
        SELECT sku::text sku, sum(free_to_sell) free_fbo
          FROM ozon_fbo_stock
         WHERE account=%(a)s
           AND captured_at=(SELECT max(captured_at) FROM ozon_fbo_stock WHERE account=%(a)s)
         GROUP BY 1)
    SELECT coalesce(c.cid, b.cid) cid, pt.sku::text sku, pt.offer_id, coalesce(pt.name,'') name,
           b.camp, b.bid,
           coalesce(c.views,0) w_views, coalesce(c.clicks,0) w_clicks,
           coalesce(c.spend,0) w_spend, coalesce(c.orders,0) w_orders,
           coalesce(c.rev,0) w_rev, coalesce(c.days,0) w_days, c.bid_avg w_bid,
           coalesce(v.views,0) p_views, coalesce(v.clicks,0) p_clicks,
           coalesce(v.spend,0) p_spend, coalesce(v.orders,0) p_orders,
           coalesce(v.rev,0) p_rev, v.bid_avg p_bid,
           coalesce(o.demand,0) o_demand, coalesce(o.views,0) o_views,
           coalesce(o.gmv,0) o_gmv, o.pos o_pos,
           coalesce(op.views,0) op_views, coalesce(op.gmv,0) op_gmv, op.pos op_pos,
           coalesce(ph.ph_demand,0) ph_demand, ph.ph_best, coalesce(ph.ph_top10,0) ph_top10,
           coalesce(ph.ph_n,0) ph_n,
           coalesce(s.qty_w,0) qty_w, coalesce(s.rev_w,0) rev_w, coalesce(s.qty_p,0) qty_p,
           s.last_sale, m.our_price, m.margin_own_live, coalesce(st.free_fbo,0) free_fbo,
           coalesce(ms.ms_stock,0) ms_stock, coalesce(sa.qty_all,0) qty_all,
           coalesce(cu.clicks,0) c_clicks, coalesce(cu.spend,0) c_spend,
           coalesce(cu.orders,0) c_orders, coalesce(cu.days,0) c_days
      FROM ozon_product pt
      LEFT JOIN bids     b  ON b.sku  = pt.sku::text
      LEFT JOIN cur      c  ON c.sku  = pt.sku::text AND (b.cid IS NULL OR c.cid = b.cid)
      LEFT JOIN prv      v  ON v.sku  = pt.sku::text AND v.cid = coalesce(c.cid, b.cid)
      LEFT JOIN org      o  ON o.sku  = pt.sku::text
      LEFT JOIN org_prev op ON op.sku = pt.sku::text
      LEFT JOIN ph       ph ON ph.sku = pt.sku::text
      LEFT JOIN sold     s  ON s.sku  = pt.sku::text
      LEFT JOIN sold_all sa ON sa.sku = pt.sku::text
      LEFT JOIN marg     m  ON m.sku  = pt.sku::text
      LEFT JOIN stock    st ON st.sku = pt.sku::text
      LEFT JOIN ms       ms ON ms.article = pt.offer_id
      LEFT JOIN cum      cu ON cu.sku = pt.sku::text AND cu.cid = coalesce(c.cid, b.cid)
     WHERE pt.account=%(a)s AND NOT pt.is_archived
    """, {'a': acc, 'w0': w0, 'w1': w1, 'p0': p0, 'p1': p1, 'sw': sw})

    for r in rows:
        cl, sp = f2(r['w_clicks']), f2(r['w_spend'])
        r['cpc'] = round(sp / cl, 1) if cl else None
        r['ctr'] = round(100 * cl / f2(r['w_views']), 2) if f2(r['w_views']) else None
        r['drr'] = round(100 * sp / f2(r['w_rev']), 1) if f2(r['w_rev']) else None
        r['p_cpc'] = round(f2(r['p_spend']) / f2(r['p_clicks']), 1) if f2(r['p_clicks']) else None
        r['in_ads'] = r['bid'] is not None
        mg = f2(r['margin_own_live'])
        r['margin_safe'] = round(mg - MARGIN_HAIRCUT, 1) if r['margin_own_live'] is not None else None
        pr = f2(r['our_price'])
        # конверсия: справочная по ценовой корзине — только пока нет своей.
        # Накопили 30+ кликов и 3+ заказа — считаем по факту (в коридоре ×0.3…×3
        # от справочной, чтобы единичный всплеск не поднял потолок до неба).
        # Иначе потолок режет позиции, которые реально окупаются: TK-5370 при ДРР 3 %
        # получал «потолок» 21.6 ₽ на ставке 60 ₽ только потому, что справочная CR 3 %,
        # а фактическая — 12 %.
        cr_base = CR[price_bucket(pr)] if pr > 0 else None
        c_cl_, c_or_ = f2(r['c_clicks']), f2(r['c_orders'])
        if cr_base and c_cl_ >= 30 and c_or_ >= 3:
            r['cr_used'] = round(min(max(c_or_ / c_cl_, cr_base * 0.3), cr_base * 3), 4)
            r['cr_src'] = 'факт'
        else:
            r['cr_used'] = cr_base
            r['cr_src'] = 'справочная'
        if r['margin_safe'] is not None and pr > 0:
            r['ceiling'] = round(max(0.0, (pr * r['margin_safe'] / 100 - pr * PPO_RATE) * r['cr_used']), 1)
        else:
            r['ceiling'] = None
        bid = f2(r['bid'])
        r['headroom'] = round(r['ceiling'] - bid, 1) if (r['ceiling'] is not None and bid) else None
        r['headroom_pct'] = round(100 * r['headroom'] / bid, 0) if (r['headroom'] is not None and bid) else None
        # органика: динамика позиции и просмотров неделя к неделе
        r['d_pos'] = r1(f2(r['o_pos']) - f2(r['op_pos'])) if (r['o_pos'] and r['op_pos']) else None
        r['d_org_views'] = int(f2(r['o_views']) - f2(r['op_views']))
        # в наличии: остаток на любом складе либо факт продажи за неделю
        r['avail'] = (f2(r['free_fbo']) + f2(r['ms_stock'])) > 0 or f2(r['qty_w']) > 0
        r['c_cpc'] = round(f2(r['c_spend']) / f2(r['c_clicks']), 1) if f2(r['c_clicks']) else None
    return rows, sw, (w0, w1, p0, p1)


def history(acc):
    """Память: что мы уже делали по этому SKU и чем это кончилось."""
    ensure_journal()
    h = {}
    for r in db.query("""SELECT campaign_id, sku, week_start, tier, action, outcome, bid_after
                           FROM mkt_ozon_bid_journal WHERE account=%s
                          ORDER BY week_start""", (acc,)):
        h.setdefault((r['campaign_id'], r['sku']), []).append(r)
    return h


def classify(r, hist):
    """(тир, действие, новая ставка, причина). Только факт недели: заказы и ДРР."""
    bid = f2(r['bid'])
    cl, sp, orders = f2(r['w_clicks']), f2(r['w_spend']), f2(r['w_orders'])
    p_orders = f2(r['p_orders'])
    drr = r['drr']
    prev = hist[-1] if hist else None
    floor_weeks = sum(1 for h in hist if h['action'] == 'floor')

    # ── не в рекламе: заводим, если товар продаётся сам
    if not r['in_ads']:
        if (f2(r['qty_w']) >= 1 or f2(r['o_gmv']) > 0) and r['avail']:
            return ('enter', 'enter', BID_FLOOR,
                    f"вне рекламы, но неделю отработал сам: продажи {f2(r['qty_w']):.0f} шт, "
                    f"органика {f2(r['o_gmv']):,.0f} ₽. Заводим с пола {BID_FLOOR:.0f} ₽"
                    .replace(',', ' '))
        return (None, None, None, None)

    c_cl, c_sp, c_or, c_days = f2(r['c_clicks']), f2(r['c_spend']), f2(r['c_orders']), f2(r['c_days'])

    # ── судить не о чем: трафика за неделю почти нет и накопленного тоже
    if cl < MIN_CLICKS and sp < MIN_SPEND_JUDGE and c_sp < 150 and c_cl < MIN_CLICKS * 2:
        return ('watch', 'hold', bid,
                f"кликов за неделю {cl:.0f} (за всё время {c_cl:.0f}), расход {sp:.0f} ₽ — "
                f"наблюдаем, ставку не трогаем")

    # ── заказов нет: в пол и держим ради показов, снятие только по счётчику недель
    if orders == 0:
        new_bid = BID_FLOOR if bid > BID_FLOOR else bid
        tail = ''
        if floor_weeks + 1 >= REMOVE_WEEKS and c_or == 0:
            tail = (f". {floor_weeks + 1}-я неделя в полу без заказов — можно выносить "
                    f"на снятие, но только отдельным решением")
        return ('bordeaux', 'floor', new_bid,
                f"{cl:.0f} кликов, {sp:.0f} ₽ и ни одного заказа за неделю "
                f"(за всё время {c_or:.0f} заказ(ов) при {c_sp:.0f} ₽). Ставка в пол, "
                f"показы оставляем{tail}")

    # ── заказы есть: решает ДРР
    if drr is None:
        return ('yellow', 'hold', bid,
                f"{orders:.0f} заказ(ов) на {f2(r['w_rev']):.0f} ₽, ДРР не считается — держим")
    if drr > DRR_LIMIT:
        return ('red', 'down10', max(BID_FLOOR, round(bid * (1 - STEP_PCT / 100), 1)),
                f"{orders:.0f} заказ(ов), но ДРР {drr} % выше предела {DRR_LIMIT} % "
                f"(расход {sp:.0f} ₽ на выручку {f2(r['w_rev']):.0f} ₽) — реклама ест")
    if drr > DRR_GOOD:
        return ('yellow', 'hold', bid,
                f"{orders:.0f} заказ(ов), ДРР {drr} % — окупается, но подъедает "
                f"(коридор {DRR_GOOD}–{DRR_LIMIT} %). Держим ставку {bid} ₽")
    new_bid = round(bid * (1 + STEP_PCT / 100), 1)
    was = f", было {p_orders:.0f} заказ(ов)" if p_orders else ""
    return ('green', 'up10', new_bid,
            f"{orders:.0f} заказ(ов) на {f2(r['w_rev']):.0f} ₽, ДРР {drr} % ≤ {DRR_GOOD} %{was}; "
            f"ставка {bid} → {new_bid} ₽")


def cmd_plan(acc, w0, save):
    rows, sw, win = fetch_week(acc, w0)
    hist = history(acc)
    dec = []
    for r in rows:
        h = hist.get((r['cid'], r['sku']), []) if r['cid'] else []
        tier, act, new, why = classify(r, h)
        if not tier:
            continue
        r['tier'], r['action'], r['bid_new'], r['reason'] = tier, act, new, why
        r['d_bid'] = round(f2(new) - f2(r['bid']), 1) if r['bid'] is not None else None
        dec.append(r)

    if save:
        ensure_journal()
        today = dt.date.today()
        for r in dec:
            if r['tier'] == 'watch':
                continue        # наблюдение — не решение, в память не пишем
            m = {k: (float(r[k]) if isinstance(r[k], (int, float)) else r[k])
                 for k in ('w_views', 'w_clicks', 'w_spend', 'w_orders', 'w_rev', 'cpc', 'drr',
                           'p_clicks', 'p_spend', 'p_orders', 'p_rev', 'o_pos', 'o_views',
                           'o_gmv', 'o_demand', 'ph_best', 'ph_top10', 'qty_w', 'qty_all',
                           'c_clicks', 'c_spend', 'c_orders', 'ceiling', 'margin_safe')}
            m['search_week'] = str(sw) if sw else None
            db.execute("""INSERT INTO mkt_ozon_bid_journal
                  (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,
                   bid_before,bid_after,reason,m_before)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
                  decided_on=EXCLUDED.decided_on, tier=EXCLUDED.tier, action=EXCLUDED.action,
                  bid_before=EXCLUDED.bid_before, bid_after=EXCLUDED.bid_after,
                  reason=EXCLUDED.reason, m_before=EXCLUDED.m_before""",
                (today, w0, acc, r['cid'] or '', r['sku'], r['offer_id'], r['name'][:200],
                 r['tier'], r['action'], r['bid'], r['bid_new'], r['reason'], json.dumps(m, default=str)))

    # ── выгрузки
    os.makedirs(OUT, exist_ok=True)
    cols = ['tier', 'action', 'sku', 'offer_id', 'name', 'camp', 'bid', 'bid_new', 'd_bid',
            'ceiling', 'headroom', 'margin_safe', 'our_price', 'cr_used', 'cr_src',
            'w_views', 'w_clicks', 'w_spend', 'w_orders', 'w_rev', 'cpc', 'ctr', 'drr',
            'p_clicks', 'p_spend', 'p_orders', 'p_cpc',
            'o_pos', 'op_pos', 'd_pos', 'o_views', 'd_org_views', 'o_gmv', 'o_demand',
            'ph_best', 'ph_top10', 'ph_n', 'qty_w', 'rev_w', 'qty_p', 'qty_all',
            'c_clicks', 'c_spend', 'c_orders', 'ms_stock', 'free_fbo', 'reason']
    head = (f'# {acc}: неделя {win[0]:%d.%m}–{win[1]:%d.%m} против {win[2]:%d.%m}–{win[3]:%d.%m}; '
            f'органика — неделя {sw:%d.%m} (лаг Ozon); шаг ±{STEP_PCT:.0f} %, '
            f'ДРР-предел {DRR_LIMIT} %, пол {BID_FLOOR} ₽')
    files = {'weekly': dec,
             'up': [r for r in dec if r['action'] == 'up10'],
             'down': [r for r in dec if r['action'] in ('down10', 'floor')],
             'enter': [r for r in dec if r['action'] == 'enter']}
    for name, sel in files.items():
        path = f'{OUT}/ozon_{acc[3:]}_week_{name}.csv'
        with open(path, 'w', newline='') as fh:
            fh.write(head + '\n')
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for r in sorted(sel, key=lambda x: -f2(x['w_spend'])):
                w.writerow({c: r.get(c) for c in cols})

    # ── сводка
    print(f'\n{acc}: неделя {win[0]:%d.%m}–{win[1]:%d.%m} (органика — неделя {sw:%d.%m})')
    order = ['green', 'yellow', 'red', 'bordeaux', 'enter', 'watch']
    for t in order:
        sel = [r for r in dec if r['tier'] == t]
        if not sel:
            continue
        sp = sum(f2(r['w_spend']) for r in sel)
        od = sum(f2(r['w_orders']) for r in sel)
        dd = sum(f2(r['d_bid'] or 0) for r in sel)
        print(f'  {t:9s} {len(sel):5d} SKU  расход {sp:9.0f} ₽  заказы {od:5.0f}  '
              f'сдвиг ставок {dd:+8.1f} ₽')
    return dec


def cmd_seed(acc, base_week):
    """Засеять журнал тем, что уже было сделано руками: разгон 08–13.08.

    Без этого память начинается с сегодняшнего дня и первые недели решений
    принимаются вслепую. Источник — mkt_ozon_bid_step_log (что реально ушло в Ozon)."""
    ensure_journal()
    steps = db.query("""
        SELECT campaign_id::text cid, sku::text sku, min(bid_before) b0, max(bid_after) b1,
               count(*) n, min(step_date) d0, max(step_date) d1,
               count(*) FILTER (WHERE applied) ok
          FROM mkt_ozon_bid_step_log WHERE account=%s GROUP BY 1,2""", (acc,))
    if not steps:
        print(f'{acc}: в mkt_ozon_bid_step_log записей нет'); return
    names = {r['sku']: (r['offer_id'], r['name']) for r in
             db.query('SELECT sku::text sku, offer_id, name FROM ozon_product WHERE account=%s', (acc,))}
    # метрики последней ЧИСТОЙ недели до разгона — иначе review не с чем сравнивать
    base, _sw, _w = fetch_week(acc, base_week)
    mb = {(r['cid'], r['sku']): r for r in base}
    n = 0
    for s in steps:
        if not s['ok']:
            continue
        off, nm = names.get(s['sku'], (None, ''))
        b = mb.get((s['cid'], s['sku']))
        m = None
        if b:
            m = json.dumps({k: (float(b[k]) if isinstance(b[k], (int, float)) else b[k])
                            for k in ('w_views', 'w_clicks', 'w_spend', 'w_orders', 'w_rev',
                                      'cpc', 'drr', 'o_pos', 'qty_w', 'ceiling')}, default=str)
        db.execute("""INSERT INTO mkt_ozon_bid_journal
              (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,
               bid_before,bid_after,reason,m_before,applied,applied_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,'ramp','up10',%s,%s,%s,%s,true,%s)
            ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
              m_before=EXCLUDED.m_before, bid_after=EXCLUDED.bid_after""",
            (s['d0'], base_week, acc, s['cid'], s['sku'], off, (nm or '')[:200],
             s['b0'], s['b1'],
             f"ежедневный разгон +10 %/день до цели, {s['n']} шаг(ов) {s['d0']:%d.%m}–{s['d1']:%d.%m}; "
             f"основание — план от 07.08 «есть запас по марже» (политика отменена 17.08)",
             m, s['d1']))
        n += 1
    print(f'{acc}: в журнал засеяно {n} позиций разгона (база — неделя {base_week:%d.%m})')


def cmd_review(acc, w0, against=None):
    """Оценить решения недели w0 по результату следующей недели (или явно заданной)."""
    ensure_journal()
    wn = against or (w0 + dt.timedelta(days=7))
    rows, _sw, _win = fetch_week(acc, wn)
    idx = {(r['cid'], r['sku']): r for r in rows}
    jr = db.query("""SELECT * FROM mkt_ozon_bid_journal
                      WHERE account=%s AND week_start=%s""", (acc, w0))
    if not jr:
        print(f'{acc}: решений за неделю {w0:%d.%m} в журнале нет'); return
    cnt = {}
    for j in jr:
        r = idx.get((j['campaign_id'], j['sku']))
        if not r:
            outcome, note, m = 'no_signal', 'позиции нет в срезе следующей недели', None
        else:
            b = j['m_before'] or {}
            o0, o1 = f2(b.get('w_orders')), f2(r['w_orders'])
            s0, s1 = f2(b.get('w_spend')), f2(r['w_spend'])
            m = {k: (float(r[k]) if isinstance(r[k], (int, float)) else r[k])
                 for k in ('w_clicks', 'w_spend', 'w_orders', 'w_rev', 'cpc', 'drr', 'o_pos')}
            if j['action'] == 'up10':
                # платить больше имеет смысл только если заказов стало больше.
                # «как не было заказов, так и нет» — это НЕ нейтральный исход, а слитые деньги
                if o0 == 0 and o1 == 0:
                    outcome = 'not_justified' if s1 > max(s0, 50) else 'no_signal'
                elif o1 > o0:
                    outcome = 'justified' if (r['drr'] is None or r['drr'] <= DRR_LIMIT) else 'not_justified'
                elif o1 < o0:
                    outcome = 'not_justified'
                else:   # заказов столько же
                    outcome = 'justified' if s1 <= s0 * 1.1 else 'not_justified'
                note = (f'заказы {o0:.0f}→{o1:.0f}, расход {s0:.0f}→{s1:.0f} ₽, '
                        f'ДРР {r["drr"]} %')
            elif j['action'] in ('down10', 'floor'):
                saved = s0 - s1
                if o0 == 0 and o1 == 0:
                    outcome = 'justified' if saved > 0 else 'no_signal'
                elif o1 >= o0 and saved > 0:
                    outcome = 'justified'
                elif o1 < o0:
                    outcome = 'not_justified'
                else:
                    outcome = 'no_signal'
                note = (f'расход {s0:.0f}→{s1:.0f} ₽ (сэкономлено {saved:.0f}), '
                        f'заказы {o0:.0f}→{o1:.0f}')
            elif j['action'] == 'enter':
                outcome = 'justified' if o1 >= 1 else 'not_justified'
                note = f'после захода: {o1:.0f} заказ(ов) на {f2(r["w_rev"]):.0f} ₽, расход {s1:.0f} ₽'
            else:
                outcome = 'no_signal'
                note = f'держали: заказы {o0:.0f}→{o1:.0f}, расход {s0:.0f}→{s1:.0f} ₽'
        cnt[(j['action'], outcome)] = cnt.get((j['action'], outcome), 0) + 1
        db.execute("""UPDATE mkt_ozon_bid_journal SET review_on=%s, m_after=%s,
                        outcome=%s, outcome_note=%s WHERE id=%s""",
                   (dt.date.today(), json.dumps(m, default=str) if m else None,
                    outcome, note, j['id']))
    print(f'\n{acc}: разбор решений недели {w0:%d.%m} по результату {wn:%d.%m}')
    for (act, out), n in sorted(cnt.items()):
        print(f'  {act:8s} → {out:14s} {n:5d}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['plan', 'review', 'seed'])
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--week', default=None, help='понедельник недели (по умолчанию последняя полная)')
    ap.add_argument('--save', action='store_true', help='записать решения в журнал')
    ap.add_argument('--against', default=None,
                    help='review: с какой неделей сравнивать (по умолчанию следующая)')
    a = ap.parse_args()
    accs = ['oz_acc1', 'oz_acc2'] if a.account == 'all' else [a.account]
    for acc in accs:
        w0 = dt.date.fromisoformat(a.week) if a.week else last_full_week(acc)
        if a.cmd == 'plan':
            cmd_plan(acc, w0, a.save)
        elif a.cmd == 'seed':
            cmd_seed(acc, dt.date.fromisoformat(a.week) if a.week else dt.date(2026, 7, 27))
        else:
            cmd_review(acc, w0, dt.date.fromisoformat(a.against) if a.against else None)
