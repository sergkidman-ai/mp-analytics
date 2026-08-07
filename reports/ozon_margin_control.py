# поток: mkt
"""reports/ozon_margin_control.py — контроль маржи Ozon (аналог margin_control.py для ВБ).

По каждой карточке Ozon считает юнит-экономику на ЖИВОЙ себестоимости TheCartridge
(«почём купим сегодня»), рядом держит FIFO-себест из витрины fin и их расхождение:

    to_pay_u = наша цена × payout_ratio          (payout = цена − комиссия, из продаж)
    net_live = to_pay_u − логистика − хранение − приёмка − возвраты − прочее − живая себест.
    margin_own_live = 100 × net_live / наша цена     (KPI, база — НАША цена, не покупателя)

Расходы площадки на штуку — из `margin_by_sku` (fin, read-only) за последний полный месяц,
делённые на qty из постингов (у Ozon в витрине qty = NULL). У кого продаж не было — медиана
аккаунта. `other` у Ozon уже включает рекламу, баллы, подписку, эквайринг и штрафы —
отдельно НЕ добавляем (иначе двойной счёт).

Главный ответ шага 2 — предел снижения цены:
    price_at_threshold = (постоянные расходы + себест) / (payout_ratio − порог/100)
и его пересечение с ценой выхода в зелёную зону (`price_for_target` из mkt_ozon_buyer_price):
verdict = уже_зелёный / можно_снижать / не_укладывается.

Запуск: ./venv/bin/python reports/ozon_margin_control.py [all|oz_acc1|oz_acc2] [--threshold 25]
"""
import datetime
import pathlib
import statistics as st
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

ACCOUNTS = ["oz_acc1", "oz_acc2"]
DEFAULT_THRESHOLD = 25.0
SALES_DAYS = 90          # окно продаж для payout_ratio
PAYOUT_FLOOR, PAYOUT_CEIL = 0.2, 1.0    # вне этого — битая строка, не берём


def _f(v):
    return None if v is None else float(v)


def last_full_month(today):
    first = today.replace(day=1)
    prev_end = first - datetime.timedelta(days=1)
    return prev_end.replace(day=1), prev_end


def payout_ratios(account):
    """payout / цена по каждому SKU из фактических продаж + медиана аккаунта."""
    rows = db.query("""
    select fd->>'product_id' sku,
           sum((fd->>'payout')::numeric)
             / nullif(sum((fd->>'price')::numeric * (fd->>'quantity')::int), 0) pr
    from raw_ozon_posting p,
         lateral jsonb_array_elements(
             coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd
    where p.account = %s and p.status = 'delivered'
      and p.in_process_at >= current_date - %s and (fd->>'price')::numeric > 0
    group by 1
    """, (account, SALES_DAYS))
    per = {r['sku']: float(r['pr']) for r in rows
           if r['pr'] is not None and PAYOUT_FLOOR <= float(r['pr']) <= PAYOUT_CEIL}
    return per, (st.median(per.values()) if per else None)


def unit_costs(account, mfrom, mto):
    """Расходы площадки на штуку по SKU: витрина fin ÷ qty из постингов. + медианы аккаунта."""
    rows = db.query("""
    with q as (
      select fd->>'product_id' sku, sum((fd->>'quantity')::int) qty
      from raw_ozon_posting p,
           lateral jsonb_array_elements(
               coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd
      where p.account = %s and p.status = 'delivered'
        and p.in_process_at >= %s and p.in_process_at < %s + interval '1 day'
      group by 1)
    select m.article sku, q.qty,
           abs(m.logistics) / q.qty  log_u,
           abs(m.storage)   / q.qty  st_u,
           abs(m.acceptance)/ q.qty  ac_u,
           abs(m.returns_sum)/ q.qty ret_u,
           abs(m.other)     / q.qty  oth_u,
           m.cogs / q.qty            fifo_u
    from margin_by_sku m
    join q on q.sku = m.article
    where m.platform = 'ozon' and m.account = %s
      and m.period_from = %s and q.qty > 0
    """, (account, mfrom, mto, account, mfrom))
    per, med = {}, {}
    for r in rows:
        per[r['sku']] = {k: _f(r[k]) for k in ('log_u', 'st_u', 'ac_u', 'ret_u', 'oth_u', 'fifo_u')}
    for k in ('log_u', 'st_u', 'ac_u', 'ret_u', 'oth_u'):
        vals = [v[k] for v in per.values() if v[k] is not None]
        med[k] = st.median(vals) if vals else 0.0
    return per, med


def live_cost(account):
    """Живая закупочная TheCartridge по offer_id: прямой код, иначе 4-значный префикс."""
    last = db.query("select max(captured_date) d from tc_buy_price")[0]['d']
    rows = db.query("""
    select distinct on (external_code) external_code ec, buy_price, captured_date d, status
    from tc_buy_price where buy_price > 0
    order by external_code, captured_date desc
    """)
    by_code = {r['ec']: (float(r['buy_price']), r['d']) for r in rows}
    no_price = {r['ec'] for r in db.query(
        "select distinct external_code ec from tc_buy_price where buy_price is null or buy_price = 0")}
    return by_code, no_price, last


def build(account, threshold, today):
    mfrom, mto = last_full_month(today)
    snap = db.query("select max(collected_on) d from ozon_price_index where account = %s",
                    (account,))[0]['d']
    if not snap:
        print(f"{account}: снимка индекса цен нет"); return None
    pr_per, pr_med = payout_ratios(account)
    costs, cost_med = unit_costs(account, mfrom, mto)
    by_code, no_price, tc_day = live_cost(account)

    names = {r['offer_id']: r['name'] for r in db.query(
        "select distinct on (offer_id) offer_id, name from ozon_product "
        "where account = %s order by offer_id, updated_at desc nulls last", (account,))}

    src = db.query("""
    select pi.offer_id, pi.price, pi.commission_fbo_pct,
           b.sku, b.buyer_price, b.color_index, b.external_index, b.price_for_target
    from ozon_price_index pi
    left join mkt_ozon_buyer_price b
           on b.account = pi.account and b.offer_id = pi.offer_id and b.snapshot_date = pi.collected_on
    where pi.account = %s and pi.collected_on = %s and pi.price > 0
    """, (account, snap))

    t = threshold / 100.0
    rows = []
    for r in src:
        offer, price = r['offer_id'], float(r['price'])
        sku = str(r['sku']) if r['sku'] is not None else None

        # payout: свой факт → медиана аккаунта → комиссия из справочника цен
        if sku and sku in pr_per:
            payout, p_src = pr_per[sku], 'факт'
        elif pr_med:
            payout, p_src = pr_med, 'аккаунт'
        elif r['commission_fbo_pct'] is not None:
            payout, p_src = 1 - float(r['commission_fbo_pct']) / 100, 'комиссия'
        else:
            continue

        c = costs.get(sku) if sku else None
        c_src = 'факт' if c else 'аккаунт'
        get = (lambda k: c[k] if c and c[k] is not None else cost_med[k])
        log_u, st_u, ac_u, ret_u, oth_u = (get('log_u'), get('st_u'), get('ac_u'),
                                           get('ret_u'), get('oth_u'))
        fifo = c['fifo_u'] if c and c.get('fifo_u') else None

        # живая себестоимость: прямой offer_id, затем 4-значный префикс (правило FBO)
        pref = offer[:4] if offer[:4].isdigit() else None
        hit = by_code.get(offer) or (by_code.get(pref) if pref else None)
        map_src = 'offer' if by_code.get(offer) else ('prefix4' if hit else None)
        if hit:
            live, price_date = hit
            buy_status = 'ok' if price_date == tc_day else 'stale'
        else:
            live, price_date = None, None
            buy_status = 'no_price' if (offer in no_price or (pref and pref in no_price)) \
                else 'unmapped'

        cogs = live if live is not None else fifo
        cogs_src = 'живая' if live is not None else ('fifo' if fifo else None)

        fixed = log_u + st_u + ac_u + ret_u + oth_u
        to_pay = price * payout
        net_live = (to_pay - fixed - live) if live is not None else None
        net_fifo = (to_pay - fixed - fifo) if fifo is not None else None
        m_live = 100 * net_live / price if net_live is not None else None
        m_fifo = 100 * net_fifo / price if net_fifo is not None else None

        # предел снижения: цена, при которой маржа = порогу
        p_thr = ((fixed + cogs) / (payout - t)) if (cogs is not None and payout > t) else None
        disc = (100 * (1 - p_thr / price)) if p_thr else None

        idx = float(r['external_index']) if r['external_index'] else None
        p_tgt = float(r['price_for_target']) if r['price_for_target'] else None
        tgt_disc = (100 * (1 - p_tgt / price)) if p_tgt else None
        if cogs is None:
            verdict = 'нет_себеста'
        elif idx is None:
            verdict = 'нет_индекса'
        elif p_tgt is None:
            verdict = 'уже_зелёный'
        elif p_thr is not None and p_tgt >= p_thr:
            verdict = 'можно_снижать'
        else:
            verdict = 'не_укладывается'

        rows.append(dict(
            captured_date=today, account=account, offer_id=offer,
            sku=int(sku) if sku and sku.isdigit() else None, name=names.get(offer),
            our_price=round(price, 2), buyer_price=r['buyer_price'],
            payout_ratio=round(payout, 4), payout_source=p_src, to_pay_u=round(to_pay, 2),
            logistics_u=round(log_u, 2), storage_u=round(st_u, 2), accept_u=round(ac_u, 2),
            returns_u=round(ret_u, 2), other_u=round(oth_u, 2), cost_source=c_src,
            buy_price_live=round(live, 2) if live is not None else None,
            buy_status=buy_status, buy_map_source=map_src, price_date=price_date,
            fifo_cogs_u=round(fifo, 2) if fifo else None,
            cogs_delta=round(live - fifo, 2) if (live is not None and fifo) else None,
            cogs_u=round(cogs, 2) if cogs is not None else None, cogs_source=cogs_src,
            net_live=round(net_live, 2) if net_live is not None else None,
            margin_own_live=round(m_live, 2) if m_live is not None else None,
            net_fifo=round(net_fifo, 2) if net_fifo is not None else None,
            margin_own_fifo=round(m_fifo, 2) if m_fifo is not None else None,
            below_threshold=bool(m_live is not None and m_live < threshold),
            is_negative=bool(net_live is not None and net_live < 0),
            threshold_pct=threshold,
            price_at_threshold=round(p_thr, 2) if p_thr else None,
            discount_limit_pct=round(disc, 2) if disc is not None else None,
            color_index=r['color_index'], external_index=r['external_index'],
            price_for_target=p_tgt, target_discount_pct=round(tgt_disc, 2) if tgt_disc else None,
            verdict=verdict))

    db.upsert("mkt_ozon_margin_control", rows, ["captured_date", "account", "offer_id"])
    db.execute("update mkt_ozon_margin_control set built_at = now() "
               "where captured_date = %s and account = %s", (today, account))

    have = [r for r in rows if r['margin_own_live'] is not None]
    below = [r for r in have if r['below_threshold']]
    neg = [r for r in have if r['is_negative']]
    can = [r for r in rows if r['verdict'] == 'можно_снижать']
    cant = [r for r in rows if r['verdict'] == 'не_укладывается']
    med_m = st.median([r['margin_own_live'] for r in have]) if have else None
    print(f"{account}: карточек {len(rows)}, маржа посчитана у {len(have)} "
          f"(медиана {med_m:.1f}% от нашей цены)" if have else f"{account}: маржи нет")
    print(f"   ниже порога {threshold:.0f}%: {len(below)}   отрицательная: {len(neg)}")
    print(f"   выход в зелёную зону укладывается в KPI: {len(can)}, "
          f"не укладывается: {len(cant)}")
    return rows


if __name__ == "__main__":
    args = sys.argv[1:]
    thr = DEFAULT_THRESHOLD
    if "--threshold" in args:
        thr = float(args[args.index("--threshold") + 1])
    acc = args[0] if args and not args[0].startswith("--") else "all"
    today = datetime.date.today()
    for a in (ACCOUNTS if acc == "all" else [acc]):
        build(a, thr, today)
