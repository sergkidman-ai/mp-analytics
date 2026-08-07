# поток: mkt
"""reports/ozon_margin_control.py — контроль маржи Ozon (аналог margin_control.py для ВБ).

По каждой карточке Ozon считает юнит-экономику на ЖИВОЙ себестоимости TheCartridge
(«почём купим сегодня»), рядом держит FIFO-себест из витрины fin и их расхождение:

    to_pay_u = наша цена × payout_ratio          (payout = цена − комиссия, из продаж)
    net_live = to_pay_u − логистика − хранение − приёмка − возвраты − прочее − живая себест.
    margin_own_live = 100 × net_live / наша цена     (KPI, база — НАША цена, не покупателя)

Расходы площадки на штуку — из `margin_by_sku` (fin, read-only) за последний полный месяц,
делённые на qty из постингов (у Ozon в витрине qty = NULL). Своих продаж за месяц нет у
21 905 карточек из 22 413 — им расходы МОДЕЛИРУЮТСЯ, и модель у каждой статьи своя
(медиана аккаунта на всё подряд давала минус там, где его нет):

    логистика  привязана к ОБЪЁМУ короба (`ozon_dims`), а не к цене: по факту 6 месяцев
               логистика = a + b × литры (a ≈ 78 ₽, b ≈ 6 ₽/л), коэффициенты считаются
               из фактических отправлений на каждом прогоне. Струйник 0.5 л стоит 77 ₽,
               а не 134 ₽ медианы каталога, где 8+ литров — лазерные и наборы;
    прочее     это не рубли, а ПРОЦЕНТ от цены: Premium Pro 2.5 % + Звёздные товары 1.5 % +
               эквайринг 0.6 % + реклама + штрафы. Ставка считается из транзакций: сумма
               этих операций ÷ оборот аккаунта. На чеке 5 100 ₽ выходит ~420 ₽, на карточке
               за 400 ₽ — ~33 ₽; плоские 188 ₽ убивали весь дешёвый хвост;
    возвраты   резерв = стоимость обработки возврата ÷ отгруженные штуки (единицы рублей).
               Строку «Получение возврата, отмены, невыкупа» из `margin_by_sku` НЕ берём:
               это сторно ВЫРУЧКИ за товар, проданный в другом месяце, а не расход за штуку;
               делённое на qty месяца оно давало до 9 926 ₽/шт на живых карточках.

`other` у Ozon уже включает рекламу, баллы, подписку, эквайринг и штрафы — отдельно
НЕ добавляем (иначе двойной счёт).

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
MODEL_DAYS = 200         # окно факта для моделей логистики / «прочего» / возвратов
PAYOUT_FLOOR, PAYOUT_CEIL = 0.2, 1.0    # вне этого — битая строка, не берём
LOG_FALLBACK = (78.0, 6.0)   # логистика = база ₽ + ₽/литр, если факта не хватило
LOG_FLOOR = 63.0             # минимум, который Ozon выставлял за отправление (факт 6 мес)
OTHER_RATE_FALLBACK = 0.09   # доля «прочего» от нашей цены, если транзакций нет

# Операции, которые НЕ попадают в «прочее»: выручка, сторно выручки, компенсации Ozon
# и возвраты (у них свой резерв). Всё остальное с минусом — это и есть «прочее».
OTHER_SKIP = ('Доставка покупателю',
              'Получение возврата, отмены, невыкупа от покупателя',
              'Перечисление за доставку от покупателя',
              'Доставка и обработка возврата, отмены, невыкупа',
              'Потеря по вине Ozon в логистике',
              'Брак по вине Ozon на складе')
RETURN_OPS = ('Доставка и обработка возврата, отмены, невыкупа',)


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


def logistics_model(account):
    """Логистика = база + ставка × литры, коэффициенты из факта отправлений за MODEL_DAYS.

    Считаем по МЕДИАНАМ объёмных вёдер, а не МНК по всем точкам: одиночные отправления
    с раздутым объёмом (наборы фотобарабанов заявлены на 34 л) утягивают прямую вверх,
    и струйник получал бы +12 ₽ из воздуха. Берутся только отправления РОВНО с одной
    единицей товара — иначе к объёму одной карточки приписана логистика всей коробки.
    """
    rows = db.query("""
    with tx as (
      select t.payload->'posting'->>'posting_number' pn,
             abs(sum((s->>'price')::numeric)) log
      from raw_ozon_transaction t,
           lateral jsonb_array_elements(coalesce(t.payload->'services', '[]'::jsonb)) s
      where t.account = %s and (t.payload->>'operation_date')::date >= current_date - %s
        and s->>'name' = 'MarketplaceServiceItemDirectFlowLogistic'
      group by 1),
    pq as (
      select distinct on (p.payload->>'posting_number') p.payload->>'posting_number' pn,
             (select sum((fd->>'quantity')::int) from jsonb_array_elements(
                 coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd) qty,
             (select fd->>'product_id' from jsonb_array_elements(
                 coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd limit 1) sku
      from raw_ozon_posting p where p.account = %s)
    select d.volume_l v, tx.log
    from tx join pq on pq.pn = tx.pn
    join ozon_dims d on d.account = %s and d.sku::text = pq.sku
    where pq.qty = 1 and d.volume_l > 0 and tx.log > 0
    """, (account, MODEL_DAYS, account, account))
    buckets = [(0, 1), (1, 2), (2, 4), (4, 8), (8, 10 ** 6)]
    pts = []
    for lo, hi in buckets:
        g = [(float(r['v']), float(r['log'])) for r in rows if lo <= float(r['v']) < hi]
        if len(g) >= 20:
            pts.append((st.median([x for x, _ in g]), st.median([y for _, y in g])))
    if len(pts) < 3:
        return LOG_FALLBACK + (0,)
    n = len(pts)
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    den = n * sum(x * x for x, _ in pts) - sx * sx
    if den <= 0:
        return LOG_FALLBACK + (len(rows),)
    k = (n * sum(x * y for x, y in pts) - sx * sy) / den
    a = (sy - k * sx) / n
    if not (40 <= a <= 150 and 0 < k <= 30):        # оценка уехала — не доверяем
        return LOG_FALLBACK + (len(rows),)
    return a, k, len(rows)


def other_rate(account):
    """Доля «прочего» в обороте: реклама, подписка, Звёздные, эквайринг, штрафы ÷ оборот.

    Это статьи, привязанные к ЦЕНЕ или к самому факту заказа, а не к коробке, поэтому
    моделируются процентом. Окно — MODEL_DAYS, чтобы разовый месяц не задирал ставку.
    """
    r = db.query("""
    select abs(sum((payload->>'amount')::numeric)
               filter (where (payload->>'amount')::numeric < 0
                         and payload->>'operation_type_name' <> all(%s))) oth,
           sum((payload->>'accruals_for_sale')::numeric)
               filter (where payload->>'operation_type_name' = 'Доставка покупателю') gross
    from raw_ozon_transaction
    where account = %s and (payload->>'operation_date')::date >= current_date - %s
    """, (list(OTHER_SKIP), account, MODEL_DAYS))[0]
    if not r['gross'] or not r['oth']:
        return OTHER_RATE_FALLBACK
    return float(r['oth']) / float(r['gross'])


def returns_provision(account):
    """Резерв на возврат, ₽/шт: стоимость обработки возвратов ÷ отгруженные штуки.

    Именно обработка (обратная логистика), а не сторно выручки: физический возврат стоит
    порядка 126 ₽ на случай, и при доле возвратов в проценты это единицы рублей на штуку.
    """
    ret = db.query("""
    select abs(sum((payload->>'amount')::numeric)) s from raw_ozon_transaction
    where account = %s and (payload->>'operation_date')::date >= current_date - %s
      and payload->>'operation_type_name' = any(%s)
    """, (account, MODEL_DAYS, list(RETURN_OPS)))[0]['s']
    qty = db.query("""
    select sum((fd->>'quantity')::int) q
    from raw_ozon_posting p, lateral jsonb_array_elements(
             coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd
    where p.account = %s and p.status = 'delivered'
      and p.in_process_at >= current_date - %s
    """, (account, MODEL_DAYS))[0]['q']
    return (float(ret) / float(qty)) if (ret and qty) else 0.0


def sku_bridge(account):
    """offer_id → sku. В `mkt_ozon_buyer_price` sku пустой у ВСЕХ строк, из-за чего факт
    по SKU (payout, расходы, габариты) не находился ни разу и всё считалось медианой.
    Мост берём из справочника карточек: на offer_id бывает несколько sku (FBO/FBS) —
    берём последнюю обновлённую."""
    return {r['offer_id']: str(r['sku']) for r in db.query(
        "select distinct on (offer_id) offer_id, sku from ozon_product "
        "where account = %s and sku is not null order by offer_id, updated_at desc nulls last",
        (account,))}


def volumes(account):
    """Объём короба по SKU из карточек Ozon + медиана каталога для тех, у кого его нет."""
    rows = db.query("select sku::text sku, volume_l from ozon_dims "
                    "where account = %s and volume_l > 0", (account,))
    per = {r['sku']: float(r['volume_l']) for r in rows}
    return per, (st.median(per.values()) if per else 2.0)


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
    # медиана остаётся только у копеечных статей — хранения и приёмки. Логистику, «прочее»
    # и возвраты медианой не заполняем: они и давали ложный минус (см. шапку модуля).
    for k in ('st_u', 'ac_u'):
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
    log_a, log_k, log_n = logistics_model(account)
    oth_rate = other_rate(account)
    ret_res = returns_provision(account)
    vol_per, vol_med = volumes(account)
    bridge = sku_bridge(account)
    print(f"   модель расходов: логистика {log_a:.0f} ₽ + {log_k:.1f} ₽/л "
          f"(по {log_n} отправлениям), прочее {100 * oth_rate:.1f}% цены, "
          f"резерв возврата {ret_res:.1f} ₽/шт, объём по умолчанию {vol_med:.1f} л")

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
        sku = str(r['sku']) if r['sku'] is not None else bridge.get(offer)

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
        c_src = 'факт' if c else 'модель'
        get = (lambda k: c[k] if c and c[k] is not None else cost_med[k])
        st_u, ac_u = get('st_u'), get('ac_u')

        # логистика: свой факт → по объёму карточки → по среднему объёму каталога
        vol = vol_per.get(sku) if sku else None
        if c and c['log_u'] is not None:
            log_u, log_src = c['log_u'], 'факт'
        else:
            log_u = max(log_a + log_k * (vol if vol else vol_med), LOG_FLOOR)
            log_src = 'объём' if vol else 'средний_объём'
        # «прочее» и возвраты — по модели для ВСЕХ: у статей-процентов свой факт по одному
        # месяцу шумит сильнее, чем ставка, а сторно выручки расходом штуки не является
        oth_u = price * oth_rate
        ret_u = ret_res
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
            volume_l=round(vol, 3) if vol else None, logistics_source=log_src,
            other_rate=round(oth_rate, 4),
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
