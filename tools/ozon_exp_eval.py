# поток: mkt
"""ozon_exp_eval.py — оценка экспериментов Ozon. ТОЛЬКО ЧТЕНИЕ.

Один инструмент на все эксперименты: состав групп берётся из реестра
docs/experiments/ozon_experiments.json, метрики — из витрин. Ничего не пишет в БД
и не ходит во внешние API; результат кладётся в docs/reports/ отдельным файлом с датой.

  snapshot --exp E8      зафиксировать состав групп в docs/experiments/cohorts/ (CSV)
  balance  --exp E8      сопоставимость групп ДО вмешательства (9 признаков)
  eval     --exp E5      treatment против control: baseline → окно, DiD, вердикт
  g6                     дневной финансовый guardrail: хвост против ядра (два уровня: YELLOW/RED)
  build-core             пересобрать ACCOUNT_STABLE_CORE по данным до 19.08 (пишет CSV один раз)

Принципы, зашитые в вывод:
  * рекламные показы (mkt_ozon_ads_sku_daily.views) и общие поисковые показы
    (ozon_search_product.unique_view_users) считаются РАЗДЕЛЬНО; вторые НЕ называются
    органическими — витрина не делит платный и бесплатный трафик;
  * прибыль не считается: margin_by_sku завышает маржу Ozon, поэтому primary profit
    помечается BLOCKED_BY_CANONICAL_MARGIN, а денежный итог называется
    «выручка минус расход», а не прибылью;
  * основной анализ — ITT: SKU остаётся в назначенной группе, даже если площадка не приняла
    изменение или по SKU нет данных; per-protocol считается вторым разрезом и только справочно;
  * при нехватке данных вердикт NO_SIGNAL, при отсутствии контроля — NO_CAUSAL_CLAIM;
  * окна выравниваются по дням недели (длина кратна 7 дням).
"""
import sys, csv, json, argparse, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
from core import db

REGISTRY = '/opt/mp-analytics/docs/experiments/ozon_experiments.json'
COHORTS = '/opt/mp-analytics/docs/experiments/cohorts'
REPORTS = '/opt/mp-analytics/docs/reports'
MIN_ORDERS = 10          # меньше — сигнала нет
MIN_DAYS = 7             # окно короче недели не оцениваем
STABLE_CORE = COHORTS + '/ACCOUNT_STABLE_CORE_2026-08-20.csv'
G6 = {'хвост_yellow': 0.15, 'хвост_red': 0.25, 'ядро_yellow': 0.90, 'ядро_red': 0.70}


def registry():
    return json.load(open(REGISTRY, encoding='utf-8'))


def exp_by_id(eid):
    for e in registry()['эксперименты']:
        if e['id'] == eid.upper():
            return e
    raise SystemExit(f'нет эксперимента {eid} в реестре')


def _actions(side):
    if not side:
        return []
    a = side['action']
    return a if isinstance(a, list) else [a]


def not_applied(exp, side_name):
    """SKU, назначенные в группу, но не получившие воздействия (площадка отклонила).

    Из анализа они НЕ исключаются: основной разрез ITT считает их в назначенной группе.
    """
    return {x['sku']: x for x in exp.get('не_применено', []) if x.get('назначен') == side_name}


def cohort(exp, side_name, mode='itt'):
    """Состав группы: (account, sku) из журнала решений по action.

    mode='itt'  — все назначенные SKU (основной разрез);
    mode='pp'   — per-protocol: без SKU, по которым воздействие не применилось.
    Переносить SKU в другую группу нельзя ни в одном режиме.
    """
    side = exp.get(side_name)
    acts = _actions(side)
    if not acts:
        return set()
    acc = exp.get('аккаунт') or exp.get('аккаунты', [None])[0]
    rows = db.query("""SELECT account, sku::text sku FROM mkt_ozon_bid_journal
                       WHERE account=%s AND action = ANY(%s)""", (acc, acts))
    cs = {(r['account'], r['sku']) for r in rows}
    if mode == 'pp':
        na = set(not_applied(exp, side_name))
        cs = {(a, s) for a, s in cs if s not in na}
    return cs


def _skus(cs):
    return [s for _, s in cs]


def ads(acc, skus, d0, d1):
    """Рекламные метрики группы за окно: суммы и средние на SKU."""
    if not skus:
        return {}
    r = db.query("""SELECT count(DISTINCT sku) sku_n, sum(views) views, sum(clicks) clicks,
                           sum(money_spent) spend, sum(orders_qty) orders, sum(orders_money) revenue,
                           count(DISTINCT sku) FILTER (WHERE orders_qty>0) sku_sold
                    FROM mkt_ozon_ads_sku_daily
                    WHERE account=%s AND sku::text = ANY(%s) AND stat_date BETWEEN %s AND %s""",
                 (acc, skus, d0, d1))[0]
    out = {k: float(v or 0) for k, v in r.items()}
    n = len(set(skus))
    out['sku_в_группе'] = n
    for k in ('views', 'clicks', 'spend', 'orders', 'revenue'):
        out[k + '_на_sku'] = out[k] / n if n else 0.0
    out['выручка_минус_расход'] = out['revenue'] - out['spend']
    out['выручка_минус_расход_на_sku'] = out['выручка_минус_расход'] / n if n else 0.0
    return out


def search(acc, skus, d0, d1):
    """Общие поисковые показы и позиция (ozon_search_product). НЕ органика — весь поиск.

    Витрина недельная, поэтому берём окна, ПЕРЕСЕКАЮЩИЕСЯ с интервалом, а не вложенные в него.
    """
    if not skus:
        return {}
    r = db.query("""SELECT count(DISTINCT sku) sku_n, sum(unique_view_users) view_users,
                           sum(unique_search_users) search_users, avg(position) pos,
                           sum(order_count) orders, sum(gmv) gmv,
                           min(period_start) p0, max(period_end) p1
                    FROM ozon_search_product
                    WHERE account=%s AND sku::text = ANY(%s)
                      AND period_end >= %s AND period_start <= %s""", (acc, skus, d0, d1))[0]
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    out = {k: _num(v) for k, v in r.items()}
    n = len(set(skus))
    for k in ('view_users', 'search_users', 'orders'):
        out[k + '_на_sku'] = (out.get(k) or 0) / n if n else 0.0
    return out


def stock_share(acc, skus, day):
    """Наличие: доля SKU группы с ненулевым остатком в МС на ближайший снимок не позже дня.

    Продавец ФБС-only, поэтому остатки Ozon-витрины (ozon_fbo_stock) покрывают единицы карточек;
    рабочий источник — supplier_stock, связка через offer_id = external_code.
    """
    if not skus:
        return {}
    r = db.query("""WITH snap AS (SELECT max(captured_at) c FROM supplier_stock WHERE captured_at::date <= %s),
                         st AS (SELECT external_code, sum(stock) s FROM supplier_stock, snap
                                WHERE captured_at = snap.c GROUP BY 1)
                    SELECT count(DISTINCT op.sku) n,
                           count(DISTINCT op.sku) FILTER (WHERE st.external_code IS NOT NULL) matched,
                           count(DISTINCT op.sku) FILTER (WHERE st.s > 0) pos
                    FROM ozon_product op LEFT JOIN st ON st.external_code = op.offer_id
                    WHERE op.account=%s AND op.sku::text = ANY(%s)""", (day, acc, skus))[0]
    n = r['n'] or 0
    return {'доля_с_остатком': (r['pos'] / n) if n else None,
            'sku_с_карточкой': n, 'sku_найдено_в_мс': r['matched']}


def price_stat(acc, skus, day):
    """Средняя цена группы на ближайший день сбора не позже указанного.

    ozon_price_index ведётся по product_id, а не по sku, поэтому связка идёт через offer_id
    карточки (ozon_product). Прямое сравнение sku с sku здесь даёт ноль совпадений.
    """
    if not skus:
        return {}
    r = db.query("""WITH d AS (SELECT max(collected_on) c FROM ozon_price_index
                               WHERE account=%s AND collected_on <= %s),
                         p AS (SELECT offer_id, avg(price) price FROM ozon_price_index, d
                               WHERE account=%s AND collected_on = d.c GROUP BY 1)
                    SELECT avg(p.price)::float avg_price, count(DISTINCT op.sku) n, max(d.c) AS price_day
                    FROM ozon_product op JOIN p USING(offer_id), d
                    WHERE op.account=%s AND op.sku::text = ANY(%s)""", (acc, day, acc, acc, skus))[0]
    return {'средняя_цена': r['avg_price'], 'sku_с_ценой': r['n'], 'день_цены': r['price_day']}


def campaigns_per_sku(acc, skus, day):
    """Сколько кампаний держит SKU по ближайшему снимку ставок не позже дня.

    SKU, которого в снимке нет, считается как ноль кампаний — именно так выглядит снятая
    с рекламы карточка; иначе среднее считалось бы только по оставшимся в рекламе.
    """
    if not skus:
        return {}
    r = db.query("""WITH s AS (SELECT max(captured_at) c FROM ozon_bids
                               WHERE account=%s AND captured_at::date <= %s),
                         b AS (SELECT sku::text sku, count(DISTINCT campaign_id) k FROM ozon_bids, s
                               WHERE account=%s AND captured_at = s.c GROUP BY 1)
                    SELECT count(*) FILTER (WHERE b.k IS NOT NULL) in_snap,
                           coalesce(sum(b.k), 0) total, max(s.c) AS snap_at
                    FROM unnest(%s::text[]) u(sku) LEFT JOIN b USING(sku), s""",
                 (acc, day, acc, skus))[0]
    n = len(set(skus))
    return {'кампаний_на_sku': float(r['total']) / n if n else None,
            'sku_в_снимке_ставок': r['in_snap'], 'снимок_ставок': r['snap_at']}


def _dates(exp, as_of=None):
    b0, b1 = exp['baseline'].split('..')
    start = dt.date.fromisoformat(exp['дата_вмешательства'])
    as_of = dt.date.fromisoformat(as_of) if as_of else dt.date.today() - dt.timedelta(days=1)
    days = (as_of - start).days + 1
    weeks = days // 7
    if weeks >= 1:                        # выравнивание по дням недели
        m1 = start + dt.timedelta(days=weeks * 7 - 1)
        bl0 = start - dt.timedelta(days=weeks * 7)
        bl1 = start - dt.timedelta(days=1)
    else:
        m1, bl0, bl1 = as_of, dt.date.fromisoformat(b0), dt.date.fromisoformat(b1)
    return start, m1, bl0, bl1, days, weeks


def cmd_snapshot(eid):
    exp = exp_by_id(eid)
    day = dt.date.today().isoformat()
    lines = []
    for side, grp in (('treatment', 'A'), ('control', 'B')):
        cs = sorted(cohort(exp, side))
        if not cs:
            continue
        na = not_applied(exp, side)
        path = f'{COHORTS}/{exp["id"]}_{side}_{day}.csv'
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['account', 'sku', 'assigned_group', 'applied', 'причина'])
            for a, sk in cs:
                x = na.get(sk)
                w.writerow([a, sk, grp, 'false' if x else 'true', x['причина'] if x else ''])
        lines.append(f'{exp["id"]} {side} (группа {grp}): {len(cs)} SKU назначено, '
                     f'не применилось {len(na)} -> {path}')
    print('\n'.join(lines) or 'состав не найден')
    print('состав зафиксирован из журнала на сегодня; журнал перезаписывается по ключу '
          '(week_start, account, campaign_id, sku) — снимок и есть защита от перезаписи')
    print('applied=false остаётся в назначенной группе: основной анализ ITT, перенос в контроль запрещён')


def cmd_balance(eid):
    exp = exp_by_id(eid)
    acc = exp.get('аккаунт') or exp.get('аккаунты', [None])[0]
    start = dt.date.fromisoformat(exp['дата_вмешательства'])
    b0, b1 = exp['baseline'].split('..')
    day_before = (start - dt.timedelta(days=1)).isoformat()
    out = [f'# Сопоставимость групп до вмешательства — {exp["id"]}: {exp["название"]}', '',
           f'Окно baseline: {b0}..{b1}. Признаки на день до вмешательства: {day_before}.',
           f'Единица рандомизации: {exp["единица_рандомизации"]}', '']
    rows = []
    for side in ('treatment', 'control'):
        cs = cohort(exp, side)
        if not cs:
            continue
        sk = _skus(cs)
        a, s = ads(acc, sk, b0, b1), search(acc, sk, b0, b1)
        st, pr, cp = stock_share(acc, sk, day_before), price_stat(acc, sk, day_before), campaigns_per_sku(acc, sk, day_before)
        rows.append((side, len(cs), a, s, st, pr, cp))
    out.append('| признак | ' + ' | '.join(f'{r[0]} ({r[1]} SKU)' for r in rows) + ' | отношение |')
    out.append('|---|' + '---|' * (len(rows) + 1))

    checks = []

    def line(name, vals, fmt='{:,.2f}', floor=None):
        # floor — абсолютный порог значимости на группу: ниже него разница считается шумом
        checks.append((name, vals, floor))
        cells = [fmt.format(v) if isinstance(v, (int, float)) and v is not None else '—' for v in vals]
        ratio = '—'
        if len(vals) == 2 and all(isinstance(v, (int, float)) and v for v in vals):
            ratio = f'{vals[0] / vals[1]:.2f}x'
        out.append(f'| {name} | ' + ' | '.join(cells) + f' | {ratio} |')

    line('расход на SKU, ₽', [r[2].get('spend_на_sku') for r in rows], floor=10_000)
    line('рекламные показы на SKU', [r[2].get('views_на_sku') for r in rows])
    line('клики на SKU', [r[2].get('clicks_на_sku') for r in rows])
    line('заказы на SKU', [r[2].get('orders_на_sku') for r in rows], floor=MIN_ORDERS)
    line('рекламная выручка на SKU, ₽', [r[2].get('revenue_на_sku') for r in rows], floor=50_000)
    line('общие поисковые показы на SKU (не органика)', [r[3].get('view_users_на_sku') for r in rows])
    line('средняя позиция в поиске', [r[3].get('pos') for r in rows])
    line('доля SKU с остатком', [r[4] and r[4].get('доля_с_остатком') for r in rows])
    line('средняя цена, ₽', [r[5] and r[5].get('средняя_цена') for r in rows])
    line('кампаний на SKU', [r[6] and r[6].get('кампаний_на_sku') for r in rows])
    flags = []
    for name, vals, floor in checks:
        if len(vals) != 2 or any(v is None for v in vals):
            continue
        a, b = float(vals[0]), float(vals[1])
        if a == 0 and b == 0:
            flags.append((name, 'сравнимы: признак нулевой в обеих группах'))
        elif b == 0 or a == 0:
            big = max(a, b) * (rows[0][1] if a > b else rows[1][1])
            if floor is not None and big < floor:
                flags.append((name, f'сравнимы: разница на всю группу {big:,.0f} — ниже порога шума {floor:,.0f}'))
            else:
                flags.append((name, 'РАСХОЖДЕНИЕ: признак есть только у одной группы'))
        else:
            k = a / b
            flags.append((name, f'{k:.2f}x — ' + ('сравнимы' if 0.8 <= k <= 1.25 else 'РАСХОЖДЕНИЕ')))
    bad = [f for f in flags if 'РАСХОЖДЕНИЕ' in f[1]]
    out += ['', '**Итог сопоставимости.**']
    out += [f'- {n}: {v}' for n, v in flags]
    out += ['', ('**Группы сопоставимы по всем девяти признакам.** Поправка не требуется; '
                 'основной метод сравнения — DiD на SKU.' if not bad else
                 '**Есть расхождения — состав групп НЕ трогаем.** Поправка: сравнение только по DiD '
                 '(разность приростов), все метрики нормируются на SKU, а признаки с расхождением '
                 'входят в отчёт как ковариаты и упоминаются при вердикте.')]
    out += ['', '**Как читать.** Отношение 0.8–1.25 — группы сравнимы по признаку. Вне этого коридора',
            'группы НЕ перераспределяются: фиксируется поправка — сравнение ведём по DiD (разность',
            'приростов), а метрики нормируем на SKU, а не на группу.', '']
    out.append('**Известные загрязнения:**')
    out += [f'- {x}' for x in exp.get('загрязнения', [])]
    path = f'{REPORTS}/ozon_exp_{exp["id"]}_balance_{dt.date.today()}.md'
    open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print('\n'.join(out[:20]))
    print(f'\nполный отчёт: {path}')


def _sides(exp, acc, start, m1, bl0, bl1, mode):
    """Метрики групп за baseline и окно измерения в заданном разрезе (itt|pp)."""
    sides = {}
    for side in ('treatment', 'control'):
        cs = cohort(exp, side, mode)
        if not cs:
            continue
        sk = _skus(cs)
        b0 = bl0.isoformat() if hasattr(bl0, 'isoformat') else bl0
        b1 = bl1.isoformat() if hasattr(bl1, 'isoformat') else bl1
        sides[side] = {'n': len(cs), 'base': ads(acc, sk, b0, b1),
                       'meas': ads(acc, sk, start.isoformat(), m1.isoformat()),
                       'поиск_окно': search(acc, sk, start.isoformat(), m1.isoformat())}
    return sides


def cmd_eval(eid, as_of=None, mode='itt'):
    exp = exp_by_id(eid)
    acc = exp.get('аккаунт') or exp.get('аккаунты', [None])[0]
    start, m1, bl0, bl1, days, weeks = _dates(exp, as_of)
    sides = _sides(exp, acc, start, m1, bl0, bl1, mode)
    out = [f'# Оценка {exp["id"]}: {exp["название"]}', '',
           f'Прогон: {dt.date.today()}. Вмешательство {start}. Окно измерения {start}..{m1} '
           f'({days} дн, полных недель {weeks}). Baseline {bl0}..{bl1}.',
           f'Дата окончательной сверки по контракту: {exp["дата_сверки"]}.', '',
           f'Разрез: **{"ITT — все назначенные SKU" if mode == "itt" else "PER_PROTOCOL — только применённые"}**. '
           f'Основной результат эксперимента — ITT.', '',
           'Прибыль не считается: `BLOCKED_BY_CANONICAL_MARGIN` (margin_by_sku завышает маржу Ozon).',
           'Денежный итог ниже — «выручка минус расход», это НЕ прибыль.', '']
    out.append('| метрика на SKU | ' + ' | '.join(
        f'{s} baseline | {s} окно | Δ' for s in sides) + ' |')
    out.append('|---|' + '---|' * (3 * len(sides)))
    deltas = {}
    for key, name in (('spend_на_sku', 'расход, ₽'), ('views_на_sku', 'рекламные показы'),
                      ('clicks_на_sku', 'клики'), ('orders_на_sku', 'заказы'),
                      ('revenue_на_sku', 'рекламная выручка, ₽'),
                      ('выручка_минус_расход_на_sku', 'выручка − расход, ₽')):
        cells, d = [], {}
        for s, v in sides.items():
            b, m = v['base'].get(key, 0), v['meas'].get(key, 0)
            d[s] = m - b
            cells += [f'{b:,.2f}', f'{m:,.2f}', f'{m - b:+,.2f}']
        deltas[key] = d
        out.append(f'| {name} | ' + ' | '.join(cells) + ' |')
    out.append('')
    for s, v in sides.items():
        sp = v['поиск_окно']
        out.append(f'- {s}: общие поисковые показы за окно {sp.get("view_users") or 0:,.0f} '
                   f'(НЕ органика — весь поиск), средняя позиция {sp.get("pos") or float("nan"):.1f}, '
                   f'SKU с рекламными заказами {v["meas"].get("sku_sold", 0):,.0f} из {v["n"]}')
    out.append('')
    has_control = 'control' in sides
    orders_t = sides.get('treatment', {}).get('meas', {}).get('orders', 0)
    if not has_control:
        verdict = 'NO_CAUSAL_CLAIM — контроля нет, сравнение только с собственной историей'
    elif weeks < 1 or days < MIN_DAYS:
        verdict = f'NO_SIGNAL — окно {days} дн короче недели, дни недели не выровнены'
    elif orders_t < MIN_ORDERS:
        verdict = f'NO_SIGNAL — заказов в treatment {orders_t:,.0f} < {MIN_ORDERS}'
    else:
        did = deltas['выручка_минус_расход_на_sku']['treatment'] - deltas['выручка_минус_расход_на_sku']['control']
        did_ord = deltas['orders_на_sku']['treatment'] - deltas['orders_на_sku']['control']
        did_sp = deltas['spend_на_sku']['treatment'] - deltas['spend_на_sku']['control']
        out.append(f'DiD на SKU: выручка−расход {did:+,.2f} ₽, заказы {did_ord:+.3f}, расход {did_sp:+,.2f} ₽')
        if did > 0 and did_ord >= 0:
            verdict = f'KEEP — прирост выручки минус расход {did:+,.2f} ₽/SKU при неотрицательном приросте заказов'
        elif did < 0 and did_sp > 0:
            verdict = f'ROLLBACK — расход растёт ({did_sp:+,.2f} ₽/SKU), выручка минус расход падает ({did:+,.2f} ₽/SKU)'
        else:
            verdict = 'NO_SIGNAL — знаки метрик расходятся, эффект не выделяется'
    out += ['', f'**Вердикт ({"ITT" if mode == "itt" else "PER_PROTOCOL"}): {verdict}**']
    na = [x for s_ in ('treatment', 'control') for x in not_applied(exp, s_).values()]
    if na and mode == 'itt':
        pp = _sides(exp, acc, start, m1, bl0, bl1, 'pp')
        out += ['', '## Вторичный разрез PER_PROTOCOL (справочно, не основной результат)', '',
                'Исключены SKU, по которым площадка не применила изменение — они остаются '
                'в назначенной группе основного анализа и никуда не переносятся:']
        out += [f'- {x["sku"]}: назначен в {x["назначен"]} (группа {x.get("группа", "—")}), '
                f'applied=false, причина: {x["причина"]}' for x in na]
        out.append('')
        out.append('| разрез | SKU treatment | выручка−расход на SKU, окно | заказы на SKU, окно |')
        out.append('|---|---:|---:|---:|')
        for nm, d in (('ITT (основной)', sides), ('per-protocol', pp)):
            t = d.get('treatment', {})
            out.append(f'| {nm} | {t.get("n", 0)} | '
                       f'{t.get("meas", {}).get("выручка_минус_расход_на_sku", 0):,.2f} | '
                       f'{t.get("meas", {}).get("orders_на_sku", 0):.3f} |')
    out += ['', '**Загрязнения по контракту:**']
    out += [f'- {x}' for x in exp.get('загрязнения', [])]
    path = f'{REPORTS}/ozon_exp_{exp["id"]}_eval_{dt.date.today()}.md'
    open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print('\n'.join(out[:6]))
    print(f'\nВердикт: {verdict}\nотчёт: {path}')


CORE_SQL = """
WITH a AS (SELECT sku::text sku, count(DISTINCT stat_date) FILTER (WHERE money_spent>0) ad_days,
                  sum(orders_qty) ad_orders, sum(money_spent) spend
           FROM mkt_ozon_ads_sku_daily
           WHERE account='oz_acc1' AND stat_date BETWEEN %s AND %s GROUP BY 1),
     pst AS (SELECT (p->>'sku')::text sku, sum((p->>'quantity')::numeric) units,
                    count(DISTINCT r.in_process_at::date) sale_days
             FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
             WHERE r.account='oz_acc1' AND r.in_process_at::date BETWEEN %s AND %s GROUP BY 1),
     snap AS (SELECT max(captured_at) c FROM supplier_stock WHERE captured_at::date <= %s),
     st AS (SELECT external_code, sum(stock) s FROM supplier_stock, snap
            WHERE captured_at = snap.c GROUP BY 1),
     e8 AS (SELECT DISTINCT sku::text sku FROM mkt_ozon_bid_journal
            WHERE account='oz_acc1' AND action IN ('wave1_restore','wave1_control'))
SELECT a.sku, a.ad_days, a.ad_orders::int ad_orders, a.spend::numeric(12,2) spend,
       coalesce(pst.units,0)::int units, coalesce(pst.sale_days,0) sale_days, st.s AS stock
FROM a JOIN pst USING(sku)
     LEFT JOIN ozon_product op ON op.account='oz_acc1' AND op.sku::text = a.sku
     LEFT JOIN st ON st.external_code = op.offer_id
     LEFT JOIN e8 ON e8.sku = a.sku
WHERE a.ad_days >= 4 AND pst.units >= 2 AND pst.sale_days >= 2
  AND coalesce(st.s, 1) > 0 AND e8.sku IS NULL
ORDER BY a.spend DESC
"""
CORE_W0, CORE_W1, CORE_STOCK_DAY = '2026-07-27', '2026-08-18', '2026-08-18'


def cmd_build_core(force=False):
    """ACCOUNT_STABLE_CORE — фиксированная когорта стабильного ядра acc1.

    Считается ТОЛЬКО по данным до 19.08 (до вмешательства E8) и после первой записи
    не пересобирается: иначе состав ядра подстроится под результат измерения.
    margin_by_sku не используется — она завышает маржу Ozon.
    """
    import os
    if os.path.exists(STABLE_CORE) and not force:
        raise SystemExit(f'состав уже зафиксирован: {STABLE_CORE}\n'
                         'менять после начала измерений нельзя (нужен --force и явное решение)')
    rows = db.query(CORE_SQL, (CORE_W0, CORE_W1, CORE_W0, CORE_W1, CORE_STOCK_DAY))
    with open(STABLE_CORE, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['sku', 'ad_days', 'ad_orders', 'spend',
                                          'units', 'sale_days', 'stock'])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    print(f'ACCOUNT_STABLE_CORE: {len(rows)} SKU -> {STABLE_CORE}')
    print(f'окно рекламы {CORE_W0}..{CORE_W1}; критерии: расход в >=4 днях, >=2 шт продано '
          f'в >=2 разных дня, не подтверждённый ноль остатка в МС, вне когорт E8')


def stable_core():
    """Список SKU ACCOUNT_STABLE_CORE или None, если когорта ещё не зафиксирована."""
    try:
        with open(STABLE_CORE, encoding='utf-8') as f:
            return [r['sku'] for r in csv.DictReader(f)]
    except FileNotFoundError:
        return None


def _level(share_tail, core_ratio):
    """Двухуровневый сигнал: RED важнее YELLOW, автоматической остановки нет."""
    red = share_tail > G6['хвост_red'] or (core_ratio is not None and core_ratio < G6['ядро_red'])
    yel = share_tail > G6['хвост_yellow'] or (core_ratio is not None and core_ratio < G6['ядро_yellow'])
    return 'RED' if red else ('YELLOW' if yel else 'GREEN')


def cmd_g6(day=None):
    """Дневной guardrail: не отъедает ли возвращённый хвост бюджет и показы ядра.

    Два ядра считаются РАЗДЕЛЬНО: E5_CORE_SAMPLE (40 SKU эксперимента E5, это выборка,
    а не ядро аккаунта) и ACCOUNT_STABLE_CORE (широкая когорта, зафиксированная по данным
    до 19.08). Только сообщения, никакой автоматической остановки.
    """
    reg = registry()
    g = reg['guardrail_G6']
    e8 = exp_by_id('E8')
    acc = e8['аккаунт']
    day = dt.date.fromisoformat(day) if day else dt.date.today() - dt.timedelta(days=1)
    d = day.isoformat()
    b0, b1 = e8['baseline'].split('..')
    A, B = _skus(cohort(e8, 'treatment')), _skus(cohort(e8, 'control'))
    e5 = exp_by_id('E5')
    sample = _skus(cohort(e5, 'treatment') | cohort(e5, 'control'))
    wide = stable_core()
    day_a, day_b = ads(acc, A, d, d), ads(acc, B, d, d)
    tot = db.query("""SELECT sum(money_spent) s FROM mkt_ozon_ads_sku_daily
                      WHERE account=%s AND stat_date=%s""", (acc, d))[0]
    spend_all = float(tot['s'] or 0)
    share_tail = (day_a.get('spend', 0) / spend_all) if spend_all else 0

    cores = [('E5_CORE_SAMPLE', sample)]
    cores.append(('ACCOUNT_STABLE_CORE', wide))
    blocks, ratios = [], []
    for name, sk in cores:
        if sk is None:
            blocks.append((name, None, None, None))
            continue
        cur, base = ads(acc, sk, d, d), ads(acc, sk, b0, b1)
        ratio = (cur.get('views', 0) / (base.get('views', 0) / 7)) if base.get('views') else None
        blocks.append((name, sk, cur, ratio))
        ratios.append(ratio)
    worst = min([r for r in ratios if r is not None], default=None)
    level = _level(share_tail, worst)
    out = [f'# G6 — бюджет ядра против возвращённого хвоста, день {d}', '',
           f'**Сигнал: {level}.** Пороги: хвост YELLOW >{G6["хвост_yellow"]:.0%} / '
           f'RED >{G6["хвост_red"]:.0%}; показы ядра YELLOW <{G6["ядро_yellow"]:.0%} / '
           f'RED <{G6["ядро_red"]:.0%} дневного baseline {b0}..{b1}.', '',
           '| показатель | значение |', '|---|---|',
           f'| расход аккаунта, ₽ | {spend_all:,.0f} |',
           f'| расход E8-A (возвращённый хвост), ₽ | {day_a.get("spend", 0):,.0f} |',
           f'| расход E8-B (контроль, снят), ₽ | {day_b.get("spend", 0):,.0f} |',
           f'| доля бюджета у хвоста | {share_tail:.1%} |', '']
    for name, sk, cur, ratio in blocks:
        if sk is None:
            out += [f'## {name}', '',
                    '**BLOCKED** — когорта не зафиксирована: нет '
                    f'`{STABLE_CORE}`. Пересобрать командой `build-core` '
                    'по данным до 19.08. Пока не зафиксирована, ядром аккаунта не считать.', '']
            continue
        out += [f'## {name} — {len(sk)} SKU', '',
                '| показатель | значение |', '|---|---|',
                f'| расход, ₽ | {cur.get("spend", 0):,.0f} |',
                f'| рекламные показы | {cur.get("views", 0):,.0f} |',
                f'| клики | {cur.get("clicks", 0):,.0f} |',
                f'| заказы | {cur.get("orders", 0):,.0f} |',
                f'| показы к дневному baseline | '
                f'{("%.0f%%" % (100 * ratio)) if ratio is not None else "—"} |', '']
    out += [f'Действие при сигнале: {g["действие_при_ALERT"]}.', '',
            {'GREEN': '**Норма: оба порога не превышены. Ничего не меняем.**',
             'YELLOW': '**YELLOW: мягкий порог пройден. Ставки НЕ меняем — это сигнал Сергею.**',
             'RED': '**RED: жёсткий порог пройден. Ставки НЕ меняем и эксперимент не '
                    'останавливаем автоматически — решение за Сергеем.**'}[level]]
    out += ['', 'E5_CORE_SAMPLE — это выборка эксперимента E5 (40 SKU), а не ядро всего аккаунта.']
    path = f'{REPORTS}/ozon_g6_{d}.md'
    open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print('\n'.join(out))
    print(f'\nотчёт: {path}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='оценка экспериментов Ozon — только чтение')
    ap.add_argument('cmd', choices=['list', 'snapshot', 'balance', 'eval', 'g6', 'build-core'])
    ap.add_argument('--exp', help='идентификатор эксперимента (E5..E8)')
    ap.add_argument('--as-of', help='оценивать по данным на дату (по умолчанию вчера)')
    ap.add_argument('--date', help='g6: день отчёта (по умолчанию вчера)')
    ap.add_argument('--mode', choices=['itt', 'pp'], default='itt',
                    help='eval: разрез анализа, по умолчанию ITT (основной)')
    ap.add_argument('--force', action='store_true', help='build-core: пересобрать зафиксированный состав')
    a = ap.parse_args()
    if a.cmd == 'list':
        for e in registry()['эксперименты']:
            print(f'{e["id"]:4s} {e.get("статус", "активен"):12s} {e["название"]} '
                  f'(сверка {e.get("дата_сверки", "—")})')
    elif a.cmd == 'g6':
        cmd_g6(a.date)
    elif a.cmd == 'build-core':
        cmd_build_core(a.force)
    else:
        if not a.exp:
            raise SystemExit('нужен --exp')
        {'snapshot': lambda: cmd_snapshot(a.exp), 'balance': lambda: cmd_balance(a.exp),
         'eval': lambda: cmd_eval(a.exp, a.as_of, a.mode)}[a.cmd]()
