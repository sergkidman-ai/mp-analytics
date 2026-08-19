#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_profile.py — недельный Рой по ПРОФИЛЮ ЦЕЛИКОМ: реклама + органика вместе.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. Правило Сергея, повторённое несколько раз: позицию судим не по
рекламному отчёту, а по профилю в целом — сколько товар суммарно заказали и сколько это стоило.
`ops/wb_roy_week.py` считал ДРР как расход/выручка_РЕКЛАМЫ и потому системно завышал его:
знаменатель был меньше настоящего. Товар, окупившийся органикой, попадал в красные.
Гало отдельно НЕ меряем (решение Сергея 17.08.2026: «гонять за призраками») — оно и не нужно,
общий знаменатель ловит и рекламный, и органический заказ одинаково.

ЧТО СЧИТАЕМ:
  выручка_всего  — sales-funnel v3 за неделю (все заказы ВБ по nm, и рекламные, и органические);
  выручка_орг    — выручка_всего − выручка_рекламы (то, что реклама притащила «мимо клика»);
  ДРР_профиля    — расход рекламы / выручка_всего. ЭТО и есть ДРР позиции;
  потолок ДРР    — индивидуальный: маржа_live − WB_MARGIN_GATE (реклама вычитается из той же
                   маржи, по которой меряется KPI-25 %).

КОГО ДОБАВЛЯТЬ. Отдельный блок: SKU, которых в рекламе НЕТ вообще (нет строки в
wb_bid_override), но по общей метрике они этого заслуживают — есть заказы за неделю и маржа
не ниже KPI. Реклама расширяется не только вглубь по уже ведомым, но и вширь.

Данные воронки за произвольную неделю в БД не лежат (wb_funnel ключуется месяцем), поэтому
скрипт читает их из JSON-выгрузки, снятой отдельно: {"w1": {nm: {...}}, "w2": {nm: {...}}}.

Запуск:
  ./venv/bin/python -m ops.wb_roy_profile <funnel_weeks.json>
"""
import csv
import io
import json
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.bid_policy import WB_MARGIN_GATE, WB_MARGIN_FLOOR  # noqa: E402
from ops import wb_roy_weeks as roy_weeks  # noqa: E402

ACC, FLOOR = 'wb_acc1', 7.30
SFX = ''          # суффикс файлов: пусто для acc1, '_wb_acc2' для второго аккаунта


def set_account(acc):
    """Переключить аккаунт целиком: запросы и имена файлов.

    У ВБ НЕТ геттера текущей ставки — источник текущих CPC это наш `wb_bid_override`, который
    заполняется только нашими же записями. На acc2 он пуст, поэтому до первой посадки ставок
    колонка «ставка_₽» там пустая, а цвета считаются по факту расхода и марже (18.08.2026)."""
    global ACC, SFX
    ACC = acc
    SFX = '' if acc == 'wb_acc1' else f'_{acc}'
# Недели считаются от воскресенья отчётной недели (--end); по умолчанию — последнее закрытое.
# Окно Джема (period_start) совпадает с началом недели день в день.
W1 = ('2026-08-03', '2026-08-09')      # прошлая неделя, пн-вс
W2 = ('2026-08-10', '2026-08-16')      # отчётная неделя, пн-вс
JAM2 = '2026-08-10'
JAM1 = '2026-08-03'


def set_weeks(end):
    """end — date воскресенья отчётной недели. Проставляет окна модуля."""
    global W1, W2, JAM1, JAM2
    w1, w2 = roy_weeks.weeks(end)
    W1 = (w1[0].isoformat(), w1[1].isoformat())
    W2 = (w2[0].isoformat(), w2[1].isoformat())
    JAM1, JAM2 = W1[0], W2[0]
CORE_MONTHS = ('2026-05-01', '2026-07-01')   # три полных месяца для ядра кандидатов


def _ads(d1, d2):
    return {r['nm_id']: r for r in db.query("""
        select nm_id, sum(views) v, sum(clicks) c, sum(orders) o,
               sum(spend) s, sum(revenue) rev
          from wb_ad_nm_daily where account=%s and dt between %s and %s group by 1""",
                                            (ACC, d1, d2))}


def _jam(period):
    return {r['nm_id']: r for r in db.query("""
        select nm_id, open_card, add_to_cart, orders, avg_position, visibility
          from wb_search_report where account=%s and period_start=%s""", (ACC, period))}


def main(path):
    fun = json.loads(pathlib.Path(path).read_text())
    f1 = {int(k): v for k, v in fun['w1'].items()}
    f2 = {int(k): v for k, v in fun['w2'].items()}
    a1, a2 = _ads(*W1), _ads(*W2)
    j2, j1 = _jam(JAM2), _jam(JAM1)
    bids = {r['nm_id']: (float(r['cpc']), r['advert_id']) for r in db.query(
        "select nm_id, cpc, advert_id from wb_bid_override where account=%s", (ACC,))}
    if not bids:
        # Аккаунт ещё не заведён в лестницу: наших записей ставок нет, геттера у ВБ нет.
        # Тогда текущая ставка берётся ФАКТОМ из открутки за неделю (расход/клики), а без
        # кликов считается полом. Это нижняя оценка ставки: факт CPC <= назначенной ставки.
        bids = {r['nm_id']: (max(FLOOR, float(r['s']) / r['c']) if r['c'] else FLOOR, r['adv'])
                for r in db.query("""select nm_id, max(advert_id) adv,
                                            sum(spend) s, sum(clicks) c
                                       from wb_ad_nm_daily
                                      where account=%s and dt between %s and %s
                                      group by nm_id""", (ACC, *W2))}
        print(f"[ставки] в wb_bid_override по {ACC} пусто → текущий CPC взят фактом "
              f"из открутки {W2[0]}–{W2[1]}: {len(bids)} SKU")
    md = db.query("select max(captured_date) d from mkt_margin_control where account=%s",
                  (ACC,))[0]['d']
    marg = {r['nm_id']: (float(r['margin_own_live']) if r['margin_own_live'] is not None else None)
            for r in db.query("""select nm_id, margin_own_live from mkt_margin_control
                                  where account=%s and captured_date=%s""", (ACC, md))}

    # ── 1. Профиль аккаунта целиком: что вообще произошло за неделю
    def tot(f):
        return (sum(x['ord'] for x in f.values()), sum(x['sum'] for x in f.values()),
                sum(x['o'] for x in f.values()), sum(x['c'] for x in f.values()))
    o1, s1, op1, c1 = tot(f1)
    o2, s2, op2, c2 = tot(f2)
    as1 = sum(float(r['s'] or 0) for r in a1.values())
    as2 = sum(float(r['s'] or 0) for r in a2.values())
    ao1 = sum(int(r['o'] or 0) for r in a1.values())
    ao2 = sum(int(r['o'] or 0) for r in a2.values())

    def d(x, y):
        return f"{100*(y-x)/x:+.0f}%" if x else "—"
    print(f"ПРОФИЛЬ ЦЕЛИКОМ · {ACC} · {W1[0]}–{W1[1]} → {W2[0]}–{W2[1]} (обе недели пн-вс)")
    print(f"{'показатель':22}{'было':>12}{'стало':>12}{'дельта':>9}")
    for lbl, x, y in (('открытия карточки', op1, op2), ('в корзину', c1, c2),
                      ('ЗАКАЗЫ всего', o1, o2), ('выручка заказов ₽', s1, s2),
                      ('из них рекл. заказы', ao1, ao2),
                      ('органические заказы', o1 - ao1, o2 - ao2),
                      ('расход рекламы ₽', as1, as2)):
        print(f"{lbl:22}{x:>12,.0f}{y:>12,.0f}{d(x, y):>9}")
    print(f"{'ДРР ПРОФИЛЯ':22}{100*as1/s1 if s1 else 0:>11.1f}%{100*as2/s2 if s2 else 0:>11.1f}%")

    # ── 2. Цвета по профилю
    rows = []
    for nm, (cpc, adv) in bids.items():
        a = a2.get(nm) or {}
        f = f2.get(nm) or {}
        fp = f1.get(nm) or {}
        j = j2.get(nm) or {}
        sp = float(a.get('s') or 0)
        ad_rev = float(a.get('rev') or 0)
        ad_o = int(a.get('o') or 0)
        tot_rev = float(f.get('sum') or 0)
        tot_o = int(f.get('ord') or 0)
        cart = int(f.get('c') or 0) or int(j.get('add_to_cart') or 0)
        org_o = max(0, tot_o - ad_o)
        org_rev = max(0.0, tot_rev - ad_rev)
        m = marg.get(nm)
        cap = None if m is None else max(0.0, m - WB_MARGIN_GATE)
        drr = (100 * sp / tot_rev) if tot_rev > 0 else None      # ДРР ПРОФИЛЯ
        at_floor = cpc <= FLOOR + 0.01
        prev_o = int(fp.get('ord') or 0)
        alive = tot_o > 0 or cart > 0

        if m is None:
            color, act, why = '⬜ маржи нет', 'наблюдаем', 'нет живой закупки'
        elif m < WB_MARGIN_FLOOR:
            color = '⚫ вывод'
            act = 'убрать' if at_floor else 'на пол → убрать'
            why = f'маржа {m:.1f}% ниже пола {WB_MARGIN_FLOOR:.0f}%'
        elif m < WB_MARGIN_GATE and sp > 0:
            color = '⚫ вывод'
            act = 'убрать' if at_floor else 'на пол → убрать'
            why = f'маржа {m:.1f}% ниже KPI → допустимый ДРР 0'
        elif sp >= 1 and not alive:
            color = '🟤 бордовый'
            act = 'на пол' if not at_floor else 'убрать'
            why = 'ноль заказов ВЕЗДЕ и ноль корзины при расходе'
        elif drr is not None and cap is not None and drr > cap:
            color = '🔴 красный'
            act = '−10%' if not at_floor else 'убрать'
            why = f'ДРР профиля {drr:.1f}% выше потолка {cap:.1f}%'
        elif m >= WB_MARGIN_GATE and tot_o > 0 and (drr is None or drr <= cap):
            color, act = '🟢 зелёный', '+10%'
            why = (f'ДРР профиля {drr:.1f}% при потолке {cap:.1f}%' if drr is not None
                   else 'заказы есть, расхода нет')
        else:
            color, act = '🟡 жёлтый', 'держим'
            why = ('в KPI, заказов нет — наблюдаем' if sp >= 1
                   else 'ставка не тратится: показы есть, кликов нет')

        rows.append(dict(nm=nm, adv=adv, cpc=cpc, color=color, act=act, why=why, m=m, cap=cap,
                         drr=drr, sp=sp, tot_rev=tot_rev, tot_o=tot_o, ad_o=ad_o, org_o=org_o,
                         org_rev=org_rev, prev_o=prev_o, cart=cart, at_floor=at_floor,
                         v=int(a.get('v') or 0), c=int(a.get('c') or 0),
                         oc=int(f.get('o') or 0), pos=float(j['avg_position']) if j.get('avg_position') else None))

    order = ['🟢 зелёный', '🟡 жёлтый', '🔴 красный', '🟤 бордовый', '⚫ вывод', '⬜ маржи нет']
    print(f"\nЦВЕТА ПО ПРОФИЛЮ · {len(rows)} SKU со ставкой · маржа на {md}")
    print(f"{'цвет':14}{'SKU':>6}{'расход ₽':>10}{'выручка ВСЯ':>13}{'ДРР проф':>9}"
          f"{'закз всего':>11}{'из них орг':>11}{'было закз':>10}")
    for k in order:
        g = [r for r in rows if r['color'] == k]
        if not g:
            continue
        sp = sum(r['sp'] for r in g)
        rv = sum(r['tot_rev'] for r in g)
        print(f"{k:14}{len(g):>6}{sp:>10,.0f}{rv:>13,.0f}"
              f"{(100*sp/rv if rv else 0):>8.1f}%{sum(r['tot_o'] for r in g):>11}"
              f"{sum(r['org_o'] for r in g):>11}{sum(r['prev_o'] for r in g):>10}")

    # ── 3. Кого ДОБАВИТЬ: продаётся сам, ставки нет вовсе
    cand = []
    for nm, f in f2.items():
        if nm in bids:
            continue
        m = marg.get(nm)
        if m is None or m < WB_MARGIN_GATE:
            continue
        tot_o = int(f.get('ord') or 0)
        if tot_o <= 0:
            continue
        rev = float(f.get('sum') or 0)
        cand.append(dict(nm=nm, m=m, cap=max(0.0, m - WB_MARGIN_GATE), o=tot_o, rev=rev,
                         prev=int((f1.get(nm) or {}).get('ord') or 0),
                         cart=int(f.get('c') or 0), oc=int(f.get('o') or 0),
                         pos=(float(j2[nm]['avg_position']) if j2.get(nm) and j2[nm].get('avg_position') else None),
                         budget=max(0.0, m - WB_MARGIN_GATE) / 100 * rev))
    cand.sort(key=lambda r: -r['budget'])
    nom = [r for r in f2.values()]
    print(f"\nКОГО ДОБАВИТЬ В РЕКЛАМУ · продаются сами, ставки нет · {len(cand)} SKU")
    print(f"  вне рекламы всего {len(set(f2) - set(bids))} nm; из них с заказами за неделю "
          f"{sum(1 for nm, f in f2.items() if nm not in bids and int(f.get('ord') or 0) > 0)}, "
          f"из них в KPI по марже {len(cand)}")
    print(f"  их заказы {sum(r['o'] for r in cand)} шт, выручка {sum(r['rev'] for r in cand):,.0f} ₽, "
          f"свободный бюджет до потолков ≈ {sum(r['budget'] for r in cand):,.0f} ₽/нед")
    print(f"  {'nm_id':>11}{'маржа%':>8}{'потолок%':>9}{'заказы':>7}{'было':>6}{'выручка ₽':>11}{'бюджет ₽':>9}")
    for r in cand[:12]:
        print(f"  {r['nm']:>11}{r['m']:>8.1f}{r['cap']:>9.1f}{r['o']:>7}{r['prev']:>6}"
              f"{r['rev']:>11,.0f}{r['budget']:>9,.0f}")

    # ── 3б. То же, но на трёх месяцах: одна неделя с одним заказом — это шум,
    # порог MIN_QTY_FACT из витрины экономики придуман ровно за этим.
    core = []
    for x in db.query("""select article, count(*) mo, sum(qty) q, sum(revenue_buyer) rev
                           from sales where platform='wb' and account=%s and granularity='month'
                            and period_from between %s and %s
                          group by 1 having sum(qty) > 0""", (ACC, *CORE_MONTHS)):
        try:
            nm = int(x['article'])
        except (TypeError, ValueError):
            continue
        m = marg.get(nm)
        if nm in bids or m is None or m < WB_MARGIN_GATE:
            continue
        mo, q, rev = int(x['mo']), float(x['q']), float(x['rev'])
        if mo < 2 or q < 3:                      # продаётся стабильно, а не разово
            continue
        core.append(dict(nm=nm, mo=mo, q=q, rev=rev, m=m, wk=(m - WB_MARGIN_GATE) / 100 * rev / 13))
    core.sort(key=lambda r: -r['rev'])
    print(f"\n  ЯДРО КАНДИДАТОВ на {CORE_MONTHS[0][:7]}–{CORE_MONTHS[1][:7]} (≥2 месяцев с продажами и ≥3 шт): "
          f"{len(core)} SKU, {sum(r['q'] for r in core):,.0f} шт, {sum(r['rev'] for r in core):,.0f} ₽; "
          f"бюджет до потолков ≈ {sum(r['wk'] for r in core):,.0f} ₽/нед")
    pk = BASE_DIR / 'docs' / 'reports' / f"mkt_roy_add_core_{W2[1]}{SFX}.csv"
    with io.open(pk, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['nm_id', 'месяцев_с_продажами', 'штук_3мес', 'выручка_3мес_₽',
                    'маржа_live_%', 'бюджет_₽_нед'])
        for r in core:
            w.writerow([r['nm'], r['mo'], f"{r['q']:.0f}", f"{r['rev']:.0f}",
                        f"{r['m']:.1f}", f"{r['wk']:.0f}"])
    print(f"  ядро: {pk}")

    pc = BASE_DIR / 'docs' / 'reports' / f"mkt_roy_add_{W2[1]}{SFX}.csv"
    with io.open(pc, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['nm_id', 'маржа_live_%', 'ДРР_потолок_%', 'заказы_неделя', 'заказы_прошлая',
                    'выручка_₽', 'бюджет_до_потолка_₽_нед', 'в_корзину', 'открытия', 'орг_позиция'])
        for r in cand:
            w.writerow([r['nm'], f"{r['m']:.1f}", f"{r['cap']:.1f}", r['o'], r['prev'],
                        f"{r['rev']:.0f}", f"{r['budget']:.0f}", r['cart'], r['oc'],
                        f"{r['pos']:.0f}" if r['pos'] else ''])
    print(f"  список: {pc}")

    p = BASE_DIR / 'docs' / 'reports' / f"mkt_roy_profile_{W2[1]}{SFX}.csv"
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['nm_id', 'advert_id', 'цвет', 'действие', 'причина', 'ставка_₽', 'маржа_live_%',
                    'ДРР_профиля_%', 'ДРР_потолок_%', 'расход_₽', 'выручка_ВСЯ_₽',
                    'выручка_органика_₽', 'заказы_всего', 'заказы_реклама', 'заказы_органика',
                    'заказы_прошлая_неделя', 'в_корзину', 'показы_рекл', 'клики_рекл',
                    'открытия_всего', 'орг_позиция'])
        for r in sorted(rows, key=lambda r: (order.index(r['color']), -r['tot_rev'])):
            w.writerow([r['nm'], r['adv'], r['color'], r['act'], r['why'], f"{r['cpc']:.2f}",
                        f"{r['m']:.1f}" if r['m'] is not None else '',
                        f"{r['drr']:.1f}" if r['drr'] is not None else '',
                        f"{r['cap']:.1f}" if r['cap'] is not None else '',
                        f"{r['sp']:.2f}", f"{r['tot_rev']:.0f}", f"{r['org_rev']:.0f}",
                        r['tot_o'], r['ad_o'], r['org_o'], r['prev_o'], r['cart'],
                        r['v'], r['c'], r['oc'],
                        f"{r['pos']:.0f}" if r['pos'] else ''])
    print(f"\nполный список: {p}")


if __name__ == '__main__':
    import argparse
    import datetime
    ap = argparse.ArgumentParser()
    ap.add_argument('path', nargs='?', default='',
                    help='json воронки за две недели; по умолчанию — по --end из docs/reports')
    ap.add_argument('--end', default='', help='воскресенье отчётной недели (по умолчанию последнее)')
    ap.add_argument('--account', default='wb_acc1', help='wb_acc1 | wb_acc2')
    a = ap.parse_args()
    set_account(a.account)
    end = datetime.date.fromisoformat(a.end) if a.end else roy_weeks.last_sunday()
    set_weeks(end)
    src = a.path or str(BASE_DIR / 'docs' / 'reports' / f'mkt_roy_funnel_{end}{SFX}.json')
    if not pathlib.Path(src).exists():
        sys.exit(f"нет воронки за неделю: {src}\nсначала: ./venv/bin/python -m ops.wb_roy_weeks "
                 f"--account {a.account} --end {end}")
    print(f"Рой по профилю: неделя {W2[0]}..{W2[1]} против {W1[0]}..{W1[1]}")
    main(src)
