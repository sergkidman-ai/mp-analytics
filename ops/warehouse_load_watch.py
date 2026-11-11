# поток: fin
"""Read-only warehouse planning watch. No writes to MS, MP or messaging APIs.

Produces a preserved snapshot, empirical receipt-time model and scenario report.
The coefficient is queue labour / available packing labour, NOT a probability.
Run: python -m ops.warehouse_load_watch --date 2026-11-10 --refresh
"""
import argparse
import collections
import csv
import datetime as dt
import html
import json
import math
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
MSK = dt.timezone(dt.timedelta(hours=3))
ORGS = {'cq': 'a5e7ee84-8d63-41f2-9fae-0be2ef90462b',
        'ds': 'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
STORES = {'e0d74d6f-db92-11ee-0a80-0cda000a40b5',
          'd9f78430-0879-11f0-0a80-08bb001470db',
          '9eeba830-29cc-446e-a369-0647bbb2ae26'}
ODYSSEY = '7225fcf1-10f7-11ea-0a80-06380004eeda'
HISTORY = ROOT / '.private/warehouse_regulation_20261107/year_analysis.json'
DIMENSIONS = ROOT / 'docs/reports/card_dimensions_own_source_plan_2026-10-15/review.csv'


def mid(obj):
    return obj.get('id') or obj.get('meta', {}).get('href', '').split('?')[0].rsplit('/', 1)[-1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def goods(doc):
    pos = doc['positions']
    if len(pos['rows']) != pos['meta']['size']:
        raise ValueError('Incomplete document positions: ' + doc['id'])
    return [p for p in pos['rows'] if p['assortment']['meta']['type'] != 'service']


def units(doc):
    return sum(float(p['quantity']) for p in goods(doc))


def minute(value):
    value = value[11:16] if len(value) > 5 else value
    hour, mins = map(int, value.split(':'))
    return hour * 60 + mins


def clock(value):
    n = int(round(value))
    return f'{n // 60:02d}:{n % 60:02d}'


def refresh(target, out):
    from core.ms_api import BASE, get
    tomorrow = (dt.date.fromisoformat(target) + dt.timedelta(days=1)).isoformat()
    planned = f'deliveryPlannedMoment>={target} 00:00:00;deliveryPlannedMoment<{tomorrow} 00:00:00'
    started = dt.datetime.now(MSK).isoformat()

    def one(task):
        org, kind = task
        datefilter = (f'moment>={target} 00:00:00;moment<{tomorrow} 00:00:00'
                      if kind == 'supply' else planned)
        filt = f'organization={BASE}/entity/organization/{ORGS[org]};{datefilter}'
        result, offset = [], 0
        while True:
            data = get('/entity/' + kind, {'filter': filt, 'limit': 20, 'offset': offset,
                       'expand': 'positions.assortment,agent,state,store'})
            save(out / 'raw' / f'{kind}_{org}_{offset}.json', data)
            result.extend(data['rows'])
            offset += len(data['rows'])
            if offset >= data['meta']['size']:
                break
            if not data['rows']:
                raise ValueError('Incomplete collection')
        for doc in result:
            if len(doc['positions']['rows']) != doc['positions']['meta']['size']:
                pos, off = [], 0
                while True:
                    data = get(f'/entity/{kind}/{doc["id"]}/positions',
                               {'limit': 100, 'offset': off, 'expand': 'assortment'})
                    save(out / 'raw' / f'positions_{doc["id"]}_{off}.json', data)
                    pos.extend(data['rows']); off += len(data['rows'])
                    if off >= data['meta']['size']:
                        break
                    if not data['rows']:
                        raise ValueError('Incomplete positions')
                doc['positions']['rows'] = pos
            goods(doc)
        if len({x['id'] for x in result}) != len(result):
            raise ValueError('Duplicate document across pages; recollect snapshot')
        return kind, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        fetched = list(pool.map(one, [(o, k) for o in ORGS
                                     for k in ('purchaseorder', 'supply', 'customerorder')]))
    result = {'target_date': target, 'started_msk': started,
              'finished_msk': dt.datetime.now(MSK).isoformat()}
    for kind in ('purchaseorder', 'supply', 'customerorder'):
        result[kind] = [r for k, rows in fetched if k == kind for r in rows]
    linked = {mid(s): s for p in result['purchaseorder'] for s in p.get('supplies', [])}
    result['linked_supplies'] = []
    for sid in linked:
        data = get('/entity/supply/' + sid, {'expand': 'positions.assortment,agent,state,store'})
        save(out / 'raw' / f'linked_supply_{sid}.json', data)
        # Never silently accept truncated linked receipts.
        goods(data)
        result['linked_supplies'].append(data)
    result['finished_msk'] = dt.datetime.now(MSK).isoformat()
    save(out / 'snapshot.json', result)
    return result


def load_dimensions(path):
    result = {}
    clean = {'Изипринт', 'Сакура', 'Рапид / Суперфайн', 'Кактус'}
    with path.open(encoding='utf-8-sig') as file:
        for row in csv.DictReader(file):
            if (row['supplier'] not in clean or row['own_source_defect_note']
                    or row['status'] not in {'NO_CHANGE_SAME_BOX', 'NO_CHANGE_FORMAT_PRECISION', 'CANDIDATE_FILL'}):
                continue
            try:
                dims = [float(x.strip().replace(',', '.')) for x in row['source_cm'].split('×')]
            except ValueError:
                continue
            if len(dims) != 3 or min(dims) <= 0 or not all(math.isfinite(x) for x in dims):
                continue
            result[row['ms_id']] = {'cm': dims, 'supplier': row['supplier'],
                'source_file': row['source_file'], 'source_sheet': row['source_sheet'],
                'source_row': row['source_row']}
    return result


def composition(orders, dimensions):
    result = []
    for doc in orders:
        large, unknown, laser_unknown, qty = [], [], [], 0
        for p in goods(doc):
            a = p['assortment']; pid = mid(a); qty += p['quantity']
            name = (a.get('name', '') + ' ' + a.get('pathName', '')).lower()
            laser = bool(re.search(r'лазер|тонер|фотобарабан|драм|\btoner\b|\blaser\b|\bdrum\b', name)) and not bool(re.search(r'струйн|чернил', name))
            size = dimensions.get(pid)
            if not size:
                unknown.append(pid)
                if laser: laser_unknown.append(pid)
            elif max(size['cm']) >= 40 or math.prod(size['cm']) >= 10000:
                large.append({'id': pid, 'name': a.get('name'), 'quantity': p['quantity'],
                              'laser': laser, **size})
        # Explicit MS flag is evidence; multiple lines alone do not prove a kit.
        bundle = any(a.get('name') == 'Бандл' and a.get('value') is True for a in doc.get('attributes', []))
        result.append({'id': doc['id'], 'name': doc['name'], 'units': qty,
                       'large': large, 'dimension_unknown_ids': unknown,
                       'laser_unknown_ids': laser_unknown, 'bundle_flag': bundle,
                       'many5': qty >= 5})
    return result


def receipt_history(history):
    groups = collections.defaultdict(list)
    for r in history['receipts']:
        if r['agent_id'] == ODYSSEY and r['store_id'] in STORES and r['org_id'] in ORGS.values():
            groups[r['date_accepted']].append(r)
    result = []
    for date, rows in sorted(groups.items()):
        accepted = [r['accepted'] for r in rows if r['org_id'] == ORGS['ds']
                    and r.get('accepted') and r['accepted'][:10] == date]
        cq = [r['accepted'] for r in rows if r['org_id'] == ORGS['cq']
              and r.get('accepted') and r['accepted'][:10] == date]
        if not accepted:
            continue
        result.append({'date': date, 'weekday': dt.date.fromisoformat(date).weekday(),
                       'month': int(date[5:7]), 'units': sum(r['units'] for r in rows),
                       'minutes': minute(max(accepted)), 'first_minutes': minute(min(accepted)),
                       'ds_units': sum(r['units'] for r in rows if r['org_id'] == ORGS['ds']),
                       'cq_minutes': minute(max(cq)) if cq else None,
                       'ds_documents': len(accepted)})
    return result


def ranks(values):
    values = np.asarray(values)
    return np.array([sum(values < x) + (sum(values == x) + 1) / 2 for x in values])


def correlation(xs, ys):
    return float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 2 and np.std(xs) and np.std(ys) else None


def features(row, volume):
    return [1.] + [float(row['weekday'] == w) for w in range(1, 7)] + ([row['units'] / 100] if volume else [])


def predict(train, row, volume):
    x = np.array([features(r, volume) for r in train[-60:]])
    y = np.array([r['minutes'] for r in train[-60:]])
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    return float(np.array(features(row, volume)) @ beta)


def model(rows, target, volume):
    # Strict chronological evaluation: held-out day's received volume is an
    # explanatory proxy, NOT a recovered previous-evening purchase-order snapshot.
    rows = [r for r in rows if r['date'] < target]
    if len(rows) < 80:
        return {'available': False, 'reason': 'insufficient accepted-state history', 'n': len(rows)}
    errors = {False: [], True: []}
    for i in range(60, len(rows)):
        for use_volume in errors:
            errors[use_volume].append(rows[i]['minutes'] - predict(rows[:i], rows[i], use_volume))
    mae = {k: float(np.mean(np.abs(v))) for k, v in errors.items()}
    selected = mae[True] < mae[False]
    future = {'weekday': dt.date.fromisoformat(target).weekday(), 'units': volume}
    center = predict(rows, future, selected)
    residuals = errors[selected]
    # Descriptive error range, not a calibrated probabilistic ETA guarantee.
    interval = [center + float(np.quantile(residuals, q)) for q in (.1, .9)]
    x = np.array([[1.] + [float(r['weekday'] == w) for w in range(1, 7)]
                  + [float(r['month'] == m) for m in range(2, 13)] + [r['units']/100] for r in rows])
    slope = float(np.linalg.lstsq(x, np.array([r['minutes'] for r in rows]), rcond=None)[0][-1])
    buckets = []
    for lo, hi in ((0, 150), (150, 300), (300, 450), (450, float('inf'))):
        group = [r for r in rows if lo <= r['units'] < hi]
        buckets.append({'from': lo, 'to': None if not math.isfinite(hi) else hi,
                        'n': len(group), 'median_minutes': float(np.median([r['minutes'] for r in group])) if group else None,
                        'after14': sum(r['minutes'] >= 840 for r in group),
                        'p_after14_observed': sum(r['minutes'] >= 840 for r in group)/len(group) if group else None})
    return {'available': True, 'n': len(rows), 'held_out_days': len(residuals),
            'period': [rows[0]['date'], rows[-1]['date']],
            'spearman': correlation(ranks([r['units'] for r in rows]), ranks([r['minutes'] for r in rows])),
            'pearson': correlation([r['units'] for r in rows], [r['minutes'] for r in rows]),
            'first_acceptance_pearson': correlation([r['units'] for r in rows], [r['first_minutes'] for r in rows]),
            'controlled_minutes_per100': slope,
            'mae_without_volume': mae[False], 'mae_with_volume': mae[True],
            'use_volume': selected, 'predicted_minutes': center, 'descriptive_error_range': interval,
            'buckets': buckets, 'multiple_ds_docs_days': sum(r['ds_documents'] > 1 for r in rows),
            'large_receipt_days': [{'date': r['date'], 'units': r['units']} for r in rows if r['units'] >= 450],
            'caveat': 'Actual daily receipt quantity is not the previous-evening PO plan. Outcome is DS acceptance, not truck arrival.'}


def queue_scenario(order_count, large_orders, ready, target, workers=3, rate=15,
                   large_extra_minutes=None, other_worker_minutes=0):
    if workers <= 0 or rate <= 0 or order_count < 0 or not 0 <= large_orders <= order_count:
        raise ValueError('Invalid queue/capacity')
    if other_worker_minutes < 0 or (large_extra_minutes is not None and large_extra_minutes < 0):
        raise ValueError('Negative additional labour')
    # Count each order once. Extra minutes are per large ORDER, not per matching SKU.
    labour = order_count * 60 / rate + large_orders * (large_extra_minutes or 0) + other_worker_minutes
    capacity = max(0, target - ready) * workers
    return {'ready': clock(ready), 'target': clock(target), 'orders': order_count,
            'worker_minutes': labour, 'available_worker_minutes': capacity,
            'coefficient': labour/capacity if capacity else None,
            'deadline_passed': target <= ready,
            'earliest_finish': clock(ready + labour/workers),
            'large_extra_minutes': large_extra_minutes,
            'large_adjustment_calibrated': large_extra_minutes is not None}


def analyze(snapshot, history, dimensions, finish='18:00', large_extra=None):
    target = snapshot['target_date']
    po = [p for p in snapshot['purchaseorder'] if mid(p.get('store', {})) in STORES]
    receipts = [s for s in snapshot['supply'] if mid(s.get('store', {})) in STORES]
    linked = {s['id']: s for s in snapshot['linked_supplies']}
    for s in receipts: linked[s['id']] = s
    po_ids = {p['id'] for p in po}
    incoming, suppliers, posted_units = 0, collections.defaultdict(float), 0
    for p in po:
        # remaining quantity is line-specific; summed goods amounts cannot cancel services
        remaining = sum(max(0, float(x['quantity'])-float(x.get('shipped', 0))) for x in goods(p))
        incoming += remaining; suppliers[mid(p['agent'])] += remaining
        if p['applicable']: posted_units += remaining
    standalone = [s for s in receipts if mid(s.get('purchaseOrder', {})) not in po_ids and not s['applicable']]
    incoming += sum(map(units, standalone))
    for s in standalone: suppliers[mid(s['agent'])] += units(s)
    co = [c for c in snapshot['customerorder'] if mid(c.get('store', {})) in STORES]
    incomplete = [c for c in co if c.get('state', {}).get('name') == 'Неукомплектован' and not c.get('demands')]
    confirmed = [c for c in co if c.get('state', {}).get('name') == 'Подтвержден' and not c.get('demands')]
    comp = composition(incomplete, dimensions)
    large = sum(bool(c['large']) for c in comp)
    hist = receipt_history(history)
    fit = model(hist, target, suppliers[ODYSSEY]) if suppliers[ODYSSEY] else {'available': False, 'reason': 'No Odyssey delivery in plan'}
    scenarios = [queue_scenario(len(incomplete), large, minute(t), minute(finish),
                 large_extra_minutes=large_extra) for t in ('13:00','14:00','15:00','16:00')]
    active = [r['receipt_units'] for r in history['days'] if r['receipt_units'] > 0]
    stats = {'incomplete_orders': len(incomplete), 'incomplete_units': sum(map(units, incomplete)),
             'confirmed_orders': len(confirmed), 'confirmed_units': sum(map(units, confirmed)),
             'large_orders': large, 'large_laser_orders': sum(any(p['laser'] for p in c['large']) for c in comp),
             'unknown_size_orders': sum(bool(c['dimension_unknown_ids']) for c in comp),
             'unknown_laser_size_orders': sum(bool(c['laser_unknown_ids']) for c in comp),
             'bundle_flag_orders': sum(c['bundle_flag'] for c in comp),
             'many5_orders': sum(c['many5'] for c in comp),
             'multiunit_orders': sum(c['units'] > 1 for c in comp)}
    flags = []
    if incoming >= float(np.quantile(active, .75)): flags.append('Приёмка выше 75-го процентиля дней с приёмкой')
    if stats['unknown_size_orders']: flags.append('Габариты части заказов не подтверждены; нагрузка может быть недооценена')
    if large and large_extra is None: flags.append('Крупные заказы найдены, добавочное время пока не измерено')
    if len(incomplete) > len(confirmed): flags.append('Большая часть текущей очереди не укомплектована')
    return {'target_date': target, 'snapshot_started': snapshot['started_msk'],
            'snapshot_finished': snapshot['finished_msk'], 'target_finish': finish,
            'target_finish_is_assumption': True, 'purchase_orders': len(po),
            'incoming_units': incoming, 'posted_purchase_remaining_units': posted_units,
            'odyssey_units': suppliers[ODYSSEY], 'receipts': len(receipts),
            'receipt_units': sum(map(units, receipts)), 'linked_old_open_receipts':
            sum(not s['applicable'] and s['moment'][:10] < target for s in linked.values()),
            'history_receipt_p75': float(np.quantile(active,.75)),
            'receipt_volume_ratio_p75': incoming/float(np.quantile(active,.75)),
            'composition': stats, 'order_details': comp, 'odyssey_model': fit,
            'scenarios': scenarios, 'flags': flags,
            'method': 'Scenario lower bound: all currently incomplete orders ready at the given time; no future orders, receiving labour, breaks or unmeasured kit/size surcharge. No proof of procurement coverage.'}


def render(result):
    e = html.escape; c = result['composition']; m = result['odyssey_model']
    rows = ''.join(f'<tr><td>{s["ready"]}</td><td>{s["coefficient"]:.2f}</td><td>{s["earliest_finish"]}</td></tr>'
                   if s['coefficient'] is not None else f'<tr><td>{s["ready"]}</td><td>нет времени</td><td>{s["earliest_finish"]}</td></tr>' for s in result['scenarios'])
    buckets = ''
    if m['available']:
        buckets = '<h3>Одиссей: объём и время принятия на Дисквэре</h3><table><tr><th>Единиц за день, оба аккаунта</th><th>Дней</th><th>Медиана</th><th>Принято с 14:00</th></tr>'
        for b in m['buckets']:
            label = str(b['from'])+'–'+str(b['to']-1) if b['to'] else 'от '+str(b['from'])
            buckets += f'<tr><td>{label}</td><td>{b["n"]}</td><td>{clock(b["median_minutes"]) if b["n"] else "—"}</td><td>{b["after14"]}/{b["n"]}</td></tr>'
        buckets += f'</table><p>{m["n"]} дней; корреляция Спирмена {m["spearman"]:.2f}. С учётом месяца и дня недели: +{m["controlled_minutes_per100"]:.0f} мин на дополнительные 100 ед. Это наблюдаемая связь, не причинный эффект.</p>'
        large_dates = '; '.join(f'{r["date"]}: {r["units"]:g} ед.' for r in m['large_receipt_days'])
        buckets += '<p><strong>Строки таблицы считают дни суммарных приёмок, а не отдельные заказы поставщику и не рейсы машины.</strong> Дни с объёмом от 450 ед.: '+e(large_dates)+'. Время — последняя приёмка Дисквэра за день; возможны несколько партий.</p>'
        buckets += f'<p>Хронологическая проверка на {m["held_out_days"]} днях: средняя ошибка {m["mae_without_volume"]:.0f} → {m["mae_with_volume"]:.0f} мин при добавлении объёма. Для текущего объёма ориентир окончания приёмки ДС — {clock(m["predicted_minutes"])}; диапазон по прошлым ошибкам {clock(m["descriptive_error_range"][0])}–{clock(m["descriptive_error_range"][1])}. Это не подтверждённое время машины и не гарантия вероятности. В истории использован фактически принятый объём, а не сохранённый вчерашний план.</p>'
    details = ''.join('<li>'+e(f'{p["name"]}: {p["quantity"]:g} ед.; '+ ' × '.join(f'{v:g}' for v in p['cm'])+f' см; {p["supplier"]}; строка {p["source_row"]}')+'</li>'
                      for o in result['order_details'] for p in o['large'])
    flags = ''.join('<li>'+e(x)+'</li>' for x in result['flags'])
    calculator = '''<div style="padding:16px;border:1px solid #8a9bb0;border-radius:10px;margin:16px 0">
<h3>Пересчитать сценарий</h3><p>Меняйте время готовности и очередь после фактического приезда. Дополнительные минуты — только ваш замер или явно условный сценарий.</p>
<label>Очередь, заказов <input id="ww-orders" type="number" min="0" value="ORDER_COUNT"></label>
<label>Готовность к сборке <input id="ww-ready" type="time" value="15:00"></label>
<label>Целевое окончание <input id="ww-end" type="time" value="FINISH_TIME"></label>
<label>Упаковщиков <input id="ww-workers" type="number" min="1" value="3"></label>
<label>Крупных заказов в этой очереди <input id="ww-large" type="number" min="0" value="LARGE_COUNT"></label>
<label>Доп. минут на крупный заказ <input id="ww-extra" type="number" min="0" placeholder="не измерено"></label>
<label>Другая работа, человеко-минут <input id="ww-other" type="number" min="0" value="0"></label>
<button type="button" onclick="warehouseWatchCalc()">Рассчитать</button><p id="ww-result" aria-live="polite"></p></div>
<script>function warehouseWatchCalc(){const el=id=>document.getElementById('ww-'+id),num=id=>Number(el(id).value),mins=t=>{const a=t.split(':').map(Number);return a[0]*60+a[1]},q=num('orders'),w=num('workers'),l=num('large'),x=num('extra'),o=num('other'),start=mins(el('ready').value),end=mins(el('end').value),out=el('result');if(!Number.isFinite(start)||!Number.isFinite(end)||q<0||w<=0||l<0||l>q||x<0||o<0){out.textContent='Проверьте очередь, крупные заказы, время и число упаковщиков.';return}const work=q*4+l*x+o,capacity=Math.max(0,end-start)*w,finish=Math.ceil(start+work/w),time=String(Math.floor(finish/60)%24).padStart(2,'0')+':'+String(finish%60).padStart(2,'0');out.textContent=(capacity?'K = '+(work/capacity).toFixed(2):'До цели нет времени')+'. Самое раннее окончание: '+time+(finish>=1440?' следующего дня':'')+'. '+(el('extra').value===''?'Добавка за крупные не учтена: замера нет. ':'Дополнительные минуты — заданный вами сценарий. ')+'Будущие заказы и неуказанная работа не включены.'}warehouseWatchCalc();</script>'''
    calculator = calculator.replace('ORDER_COUNT', str(c['incomplete_orders'])).replace('LARGE_COUNT', str(c['large_orders'])).replace('FINISH_TIME', e(result['target_finish']))
    return f'''<section id="warehouse-watch"><h2>Сторож нагрузки: {e(result['target_date'])}</h2>
<p>Срез МСК: {e(result['snapshot_started'])} — {e(result['snapshot_finished'])}. Расчёт по двум аккаунтам, без записей в МС и маркетплейсы.</p>
<p><strong>Приёмка: {result['incoming_units']:g} ед.; Одиссей: {result['odyssey_units']:g} ед.</strong> Приёмки, связанные с заказами поставщику, повторно не прибавляются. Объём включает непроведённые заказы: {result['incoming_units']-result['posted_purchase_remaining_units']:g} ед. могут требовать подтверждения; несвязанные приёмки, если есть, также входят в эту разницу.</p>
<p>Неукомплектовано: <strong>{c['incomplete_orders']} заказов / {c['incomplete_units']:g} ед.</strong> Дополнительно «Подтвержден»: {c['confirmed_orders']} / {c['confirmed_units']:g} ед. Это статусы МС, не проверка фактического свободного остатка. Полный состав неукомплектованных не равен нехватке; покрытие закупками не доказано.</p>
<p>Доля неукомплектованных в этих двух статусах: {c['incomplete_orders']/max(1,c['incomplete_orders']+c['confirmed_orders'])*100:.0f}%. Если подтверждённые действительно полностью доступны и ещё не собраны, их хватит лишь на {c['confirmed_orders']/45*60:.0f} минут работы трёх упаковщиков. Это текущая видимая очередь, а не прогноз всего утра: ночные заказы и фактическое наличие нужно обновить.</p>
<p>Состав очереди: {c['multiunit_orders']} заказов с несколькими единицами, {c['many5_orders']} по 5+ ед.; явный флаг «Бандл» у {c['bundle_flag_orders']} (цветовые наборы этим не исчерпываются). Подтверждённо крупные: не менее {c['large_orders']} заказов, из них явно лазерные {c['large_laser_orders']}. Размер хотя бы одной позиции неизвестен в {c['unknown_size_orders']} заказах, включая {c['unknown_laser_size_orders']} явно лазерных.</p>
<p>Крупный короб: сторона ≥40 см или объём ≥10 л. Это открытый порог для сигнала, не норма упаковочного времени. Только реальный короб по точному UUID из проверенного источника от 15.10; неизвестные размеры не восстанавливаются по названию, другим поставщикам или модели.</p>
<details><summary>Подтверждённые крупные позиции</summary><ul>{details or '<li>Не найдены; отсутствие подтверждения не доказывает отсутствие крупных.</li>'}</ul></details>
<h3>Коэффициент очереди на завтра — сценарии</h3>
<p>K = требуемые человеко-минуты / доступные человеко-минуты до <strong>{e(result['target_finish'])}</strong>. Время окончания пока допущение. Три упаковщика, 15 заказов/час каждый. K=1 означает заполненную мощность без запаса; K&gt;1 — текущая очередь уже не помещается. K&lt;1 не гарантирует отсутствие задержки.</p>
<table><tr><th>Вся текущая очередь доступна к сборке</th><th>K</th><th>Самое раннее окончание</th></tr>{rows}</table>
{calculator}
<p>Время в таблице — уже после приёмки, автоматической обработки и печати, не время приезда. Если ориентир — принятие на ДС, нужно дополнительно учесть завершение приёмки ЦК и 15–20 минут обработки/печати. Это условная нижняя оценка: будущие заказы, приёмка руками упаковщиков, перерывы и неизмеренная сложность добавят работу. Крупным заказам нельзя произвольно присвоить множитель.</p>
<p>Объём приёмки относительно исторического 75-го процентиля ({result['history_receipt_p75']:.0f} ед.): {result['receipt_volume_ratio_p75']:.2f}. Это отдельный индикатор приёмки, не коэффициент мощности упаковки.</p><ul>{flags}</ul>
{buckets}<h3>Действия по сигналу</h3><p>Вечером — проверить покрытие заказов закупкой, объём и габариты; согласовать окно Одиссея. Утром — обновить очередь и доступную работу до поставки. В 11:00 и 13:00 — уточнить ETA; при задержке пересчитать K по ожидаемому времени готовности товара. Если K≥1, требуется дополнительная мощность или разрешённый сроком МП перенос; приёмку выделять отдельно от упаковщиков. Автоматического переноса заказов или изменения смен нет.</p>
<p>Для полноценного сторожа нужны журнал фактического приезда, начала/конца приёмки и сборки, замер времени крупных/наборных заказов, утверждённый конец смены и снимки плана накануне. Полноценный единый коэффициент дня пока не калиброван. Автоматическая рассылка не включена.</p></section>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default=(dt.datetime.now(MSK).date()+dt.timedelta(days=1)).isoformat())
    parser.add_argument('--output', type=pathlib.Path)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--publish', action='store_true', help='Update the existing protected BI report section')
    parser.add_argument('--finish', default='18:00')
    parser.add_argument('--large-extra-minutes', type=float, default=None,
                        help='Measured extra minutes per large order; omit when unmeasured')
    args = parser.parse_args(); dt.date.fromisoformat(args.date); minute(args.finish)
    out = args.output or ROOT / '.private/warehouse_load_watch' / (args.date+'_'+dt.datetime.now(MSK).strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True, exist_ok=True)
    snapshot = refresh(args.date, out) if args.refresh else json.loads((out/'snapshot.json').read_text())
    if snapshot['target_date'] != args.date: raise ValueError('Snapshot date mismatch')
    history = json.loads(HISTORY.read_text())
    result = analyze(snapshot, history, load_dimensions(DIMENSIONS), args.finish, args.large_extra_minutes)
    save(out/'analysis.json', result)
    save(out/'odyssey_history.json', receipt_history(history))
    fragment = render(result)
    (out/'report_fragment.html').write_text(fragment)
    if args.publish:
        publish(fragment, out)
    print(json.dumps({k:result[k] for k in ['target_date','snapshot_finished','incoming_units','odyssey_units','composition','flags']}, ensure_ascii=False))
    print('Artifacts:', out)


def publish(fragment, out):
    # Static report only: no router/service/auth changes. Preserve the rest of the page.
    import fcntl
    import os
    import tempfile
    page = ROOT / 'web/static/reports/warehouse-load-2026/index.html'
    lock = ROOT / '.private/warehouse_watch_publish.lock'
    with lock.open('a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        old = page.read_text()
        (out/'published_report_before.html').write_text(old)
        if 'id="warehouse-watch"' in old:
            new, count = re.subn(r'<section id="warehouse-watch">.*?</section>', lambda _:fragment, old, flags=re.S)
            if count != 1: raise ValueError('Unexpected watcher section count')
        else:
            if old.count('</main>') != 1: raise ValueError('Unexpected report structure')
            new = old.replace('</main>', fragment+'</main>', 1)
        fd, temp = tempfile.mkstemp(dir=page.parent, prefix='.warehouse-watch-')
        try:
            with os.fdopen(fd, 'w') as file: file.write(new)
            os.chmod(temp, page.stat().st_mode & 0o777)
            os.replace(temp, page)
        finally:
            if os.path.exists(temp): os.unlink(temp)


if __name__ == '__main__':
    main()
