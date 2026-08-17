#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_profile.py — недельный Рой по ПРОФИЛЮ ЦЕЛИКОМ: реклама + органика вместе.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. Правило Сергея, повторённое несколько раз: позицию судим не по
рекламному отчёту, а по профилю в целом — сколько товар суммарно заказали и на сколько вырос
или упал ОН, а не его рекламная строка. Причина не в педантичности: на ВБ работает гало —
рекламный показ поднимает карточку в органике, и заказ приходит уже без клика по рекламе.
`ops/wb_roy_week.py` считал ДРР как расход/выручка_РЕКЛАМЫ и потому системно завышал его:
знаменатель был меньше настоящего. Товар, окупившийся органикой, попадал в красные.

ЧТО СЧИТАЕМ:
  выручка_всего  — sales-funnel v3 за неделю (все заказы ВБ по nm, и рекламные, и органические);
  выручка_орг    — выручка_всего − выручка_рекламы (то, что реклама притащила «мимо клика»);
  ДРР_профиля    — расход рекламы / выручка_всего. ЭТО и есть ДРР позиции;
  потолок ДРР    — индивидуальный: маржа_live − WB_MARGIN_GATE (реклама вычитается из той же
                   маржи, по которой меряется KPI-25 %).

ГАЛО меряем, а не постулируем: SKU режутся по изменению расхода неделя-к-неделе, и по каждой
группе сравнивается рост ОРГАНИЧЕСКИХ заказов. Если гало есть, группа с выросшим расходом
обязана расти в органике быстрее группы, которую не трогали.

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

ACC, FLOOR = 'wb_acc1', 7.30
W1 = ('2026-08-03', '2026-08-09')      # прошлая неделя, пн-вс
W2 = ('2026-08-10', '2026-08-16')      # отчётная неделя, пн-вс
JAM2 = '2026-08-10'                    # окно Джема 10-16.08 — совпадает с W2 день в день
JAM1 = '2026-08-03'


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

    # ── 2. Гало: группируем по изменению расхода, смотрим ОРГАНИКУ
    print(f"\nГАЛО-ТЕСТ. Группы по изменению расхода рекламы, рост считается по ОРГАНИЧЕСКИМ заказам")
    print(f"{'группа':26}{'SKU':>6}{'расход ₽':>10}{'орг.закз':>10}{'орг.закз':>10}"
          f"{'дельта':>9}{'открытия':>10}{'дельта':>9}")
    groups = {'расход вырос вдвое+': [], 'расход вырос': [], 'расход не менялся': [],
              'расход упал': [], 'не тратили обе недели': []}
    for nm in set(list(f1) + list(f2) + list(bids)):
        sp1 = float((a1.get(nm) or {}).get('s') or 0)
        sp2 = float((a2.get(nm) or {}).get('s') or 0)
        if sp1 < 1 and sp2 < 1:
            k = 'не тратили обе недели'
        elif sp1 < 1 <= sp2 or (sp1 and sp2 / sp1 >= 2):
            k = 'расход вырос вдвое+'
        elif sp2 > sp1 * 1.1:
            k = 'расход вырос'
        elif sp2 < sp1 * 0.9:
            k = 'расход упал'
        else:
            k = 'расход не менялся'
        groups[k].append(nm)
    for k, nms in groups.items():
        if not nms:
            continue
        sp2 = sum(float((a2.get(n) or {}).get('s') or 0) for n in nms)
        oo1 = sum((f1.get(n) or {}).get('ord', 0) - int((a1.get(n) or {}).get('o') or 0) for n in nms)
        oo2 = sum((f2.get(n) or {}).get('ord', 0) - int((a2.get(n) or {}).get('o') or 0) for n in nms)
        op_1 = sum((f1.get(n) or {}).get('o', 0) for n in nms)
        op_2 = sum((f2.get(n) or {}).get('o', 0) for n in nms)
        print(f"{k:26}{len(nms):>6}{sp2:>10,.0f}{oo1:>10}{oo2:>10}{d(oo1, oo2):>9}"
              f"{op_2:>10,}{d(op_1, op_2):>9}")

    # ── 3. Цвета по профилю
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

    p = BASE_DIR / 'docs' / 'reports' / f"mkt_roy_profile_{W2[1]}.csv"
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
    main(sys.argv[1] if len(sys.argv) > 1 else 'funnel_weeks.json')
