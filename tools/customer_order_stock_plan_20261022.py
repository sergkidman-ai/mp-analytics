# поток: inv
"""Read-only inventory and replacement plan. No methods writing to MoySklad."""
import csv
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests

from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP

OUT = Path('/opt/mp-analytics/docs/reports/customer_order_stock_plan_20261022')
TARGET_DATE = '2026-10-22 00:00:00'


def save(name, obj):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    path.chmod(0o600)


def load(name):
    return json.loads((OUT / name).read_text())


def num(x):
    return Decimal(str(x or 0))


def orders():
    return sorted([r for f in sorted(OUT.glob('orders_*.json'))
                   for r in json.loads(f.read_text())['rows']], key=lambda o: (o['moment'], o['id']))


def tc(code):
    path = OUT / 'tc_live' / (code + '.json')
    if path.exists():
        return json.loads(path.read_text())
    # POST is the catalogue's read endpoint. It does not modify orders or catalogue.
    response = requests.post('https://thecartridge.ru/api/catalog/mix_data',
        headers={'Api-Key': os.environ['CARTRIDGE_API_KEY'], 'Content-Type': 'application/json'},
        json={'external_code': code}, timeout=45)
    if response.status_code != 200:
        raise RuntimeError('TC lookup HTTP ' + str(response.status_code))
    obj = response.json()
    save('tc_live/' + code + '.json', obj)
    return obj


def bom(code, path=()):
    if code in path or len(path) > 10:
        raise ValueError('Cycle/depth in TC composition')
    obj = tc(code)
    if isinstance(obj, dict) and obj.get('error') == 'not_mix':
        return Counter({code: 1})
    if not isinstance(obj, list) or not obj:
        raise ValueError('Unconfirmed TC composition for ' + code)
    result = Counter()
    for item in obj:
        component = str(item['external_code']) if isinstance(item, dict) else str(item)
        result.update(bom(component, path + (code,)))
    return result


def get(path, params):
    start = time.monotonic()
    result = ms_api.get(path, params)
    with (OUT / 'get_calls.jsonl').open('a') as f:
        f.write(json.dumps({'method': 'GET', 'path': path, 'params': params,
            'at': datetime.now(timezone.utc).isoformat(), 'seconds': time.monotonic() - start}) + '\n')
    time.sleep(.35)
    return result


def collect():
    source_orders = orders()
    codes = sorted({r['assortment']['externalCode'] for o in source_orders for r in o['positions']['rows']})
    boms = {c: dict(bom(c)) for c in codes}
    save('boms.json', boms)
    wanted = sorted(set(codes) | {c for b in boms.values() for c in b})
    candidates = {}
    for start in range(0, len(wanted), 10):
        chunk = wanted[start:start + 10]
        cache = f'candidates/page_{start:04}.json'
        if (OUT / cache).exists():
            rows = load(cache)
        else:
            rows, offset = [], 0
            while True:
                result = get('/entity/assortment', {'limit': 100, 'offset': offset,
                    'filter': [f'externalCode={c}' for c in chunk] + ['archived=false'],
                    'expand': 'supplier'})
                rows += result['rows']
                offset += len(result['rows'])
                if offset >= result['meta']['size']:
                    break
            save(cache, rows)
        for row in rows:
            assert row['externalCode'] in chunk
            if row['meta']['type'] in ('product', 'variant'):
                candidates[row['id']] = row
    # Retain the exact current assortment even if its external code was edited mid-read.
    for o in source_orders:
        for r in o['positions']['rows']:
            candidates.setdefault(r['assortment']['id'], r['assortment'])
    save('candidates.json', candidates)
    stores = {o['store']['id']: o['store'] for o in source_orders}
    # Restrict each warehouse to the external codes needed by its own orders.
    stock = {}
    for sid, store in stores.items():
        need = {c for o in source_orders if o['store']['id'] == sid
                for r in o['positions']['rows']
                for c in [r['assortment']['externalCode'], *boms[r['assortment']['externalCode']]]}
        ids = sorted(cid for cid, c in candidates.items() if c['externalCode'] in need)
        for start in range(0, len(ids), 15):
            chunk = ids[start:start + 15]
            cache = f'stock/{sid}_{start:04}.json'
            if (OUT / cache).exists():
                rows = load(cache)
            else:
                rows, offset = [], 0
                while True:
                    result = get('/report/stock/all', {'limit': 100, 'offset': offset,
                        'filter': ['store=' + store['meta']['href'], 'stockMode=all']
                                  + ['product=' + candidates[cid]['meta']['href'] for cid in chunk]})
                    rows += result['rows']
                    offset += len(result['rows'])
                    if offset >= result['meta']['size']:
                        break
                save(cache, rows)
            for cid in chunk:
                stock[sid + '/' + cid] = {'stock': 0, 'reserve': 0, 'inTransit': 0}
            for row in rows:
                cid = row['meta']['href'].split('?')[0].rsplit('/', 1)[-1]
                assert cid in chunk, 'Stock filter returned unrelated assortment'
                stock[sid + '/' + cid] = row
        print('Warehouse stock read:', store['name'], len(ids), flush=True)
    save('stock.json', stock)
    print('Collected:', len(source_orders), 'orders;', len(boms), 'codes;',
          sum(isinstance(tc(c), list) for c in codes), 'set codes;', len(candidates), 'candidate cards', flush=True)


def brand(card):
    from prices.ms_import import abbr_by_name
    value = next((str(a['value']).strip() for a in card.get('attributes', []) if a['name'] == 'Бренд'), '')
    if value:
        return abbr_by_name(value) or {'hb': 'hb', 'ep': 'ep', 'np': 'np'}.get(value.casefold(), value.casefold()), value, 'Бренд'
    # Established supplier rule: Odyssey AT-/AQ articles and NNNNat codes are Aquamarine.
    # Require both explicit identifiers and the supplier; printer names are not brand evidence.
    article = (card.get('article') or '').upper().strip()
    is_odyssey = ID2GROUP.get(ms_api.meta_id(card, 'supplier')) == 'Одиссей' or 'ОДИССЕЙ' in card.get('supplier', {}).get('name', '').upper()
    if is_odyssey and article.startswith(('AT-', 'AQ ')) and card.get('code') == card.get('externalCode', '') + 'at':
        return 'at', 'AQUAMARINE', 'Правило Одиссея: артикул AT-/AQ и код ...at'
    value = abbr_by_name(card.get('name', ''))
    return value or '', value or '', 'Явное название бренда' if value else 'Не определён'


def supplier(card):
    sid = ms_api.meta_id(card, 'supplier')
    return ID2GROUP.get(sid, sid or ''), card.get('supplier', {}).get('name', '')


class Planner:
    def __init__(self, candidates, stock, boms, *, set_codes=None, allow_single_replacements=True):
        self.cards, self.stock, self.boms = candidates, stock, boms
        self.set_codes = set_codes
        self.allow_single_replacements = allow_single_replacements
        self.by_code = defaultdict(list)
        for cid, c in candidates.items():
            if not c.get('archived') and 'брак' not in (c.get('name', '') + ' ' + c.get('pathName', '')).casefold():
                self.by_code[c['externalCode']].append(cid)
        self.initial = {k: num(v.get('stock')) - num(v.get('reserve')) for k, v in stock.items()}

    def available(self, sid, cid, pool, used):
        key = sid + '/' + cid
        return max(Decimal(0), min(pool.get(key, Decimal(0)),
                   num(self.stock.get(key, {}).get('stock')) - used.get(key, Decimal(0))))

    def select(self, code, quantity, ids, sid, pool, used):
        need = num(quantity)
        selected = []
        ids = sorted(ids, key=lambda cid: (-self.available(sid, cid, pool, used), cid))
        for cid in ids:
            available = self.available(sid, cid, pool, used)
            # Cartridge order quantities are whole units. Do not promise fractional stock.
            available = available // 1
            take = min(need, available)
            if take > 0:
                key = sid + '/' + cid
                pool[key] = pool.get(key, Decimal(0)) - take
                used[key] = used.get(key, Decimal(0)) + take
                selected.append({'id': cid, 'external_code': code, 'quantity': float(take)})
                need -= take
            if need == 0:
                return selected
        return None

    def group_select(self, composition, groups, sid, pool, used):
        possibilities = []
        for key, ids in groups.items():
            p, u, selected = dict(pool), dict(used), []
            for code, quantity in composition.items():
                result = self.select(code, quantity, [cid for cid in ids if self.cards[cid]['externalCode'] == code], sid, p, u)
                if result is None:
                    break
                selected += result
            else:
                spare = sum(self.available(sid, cid, p, u) for cid in ids)
                possibilities.append((spare, str(key), selected, p, u))
        if not possibilities:
            return None
        _, key, selected, p, u = max(possibilities, key=lambda v: (v[0], v[1]))
        return selected, p, u, key

    def order(self, order, pool, used):
        sid = order['store']['id']
        p, u = dict(pool), dict(used)
        for row in order['positions']['rows']:
            if order.get('applicable'):
                key = sid + '/' + row['assortment']['id']
                p[key] = p.get(key, Decimal(0)) + min(num(row.get('reserve')), num(row['quantity']))
        lines = []
        for row in order['positions']['rows']:
            current, qty = row['assortment'], num(row['quantity'])
            code = current['externalCode']
            is_set = code in self.set_codes if self.set_codes is not None else isinstance(tc(code), list)
            if is_set:
                composition = {c: num(n) * qty for c, n in self.boms[code].items()}
                ids = [cid for c in composition for cid in self.by_code[c]]
                groups = defaultdict(list)
                for cid in ids:
                    s, _ = supplier(self.cards[cid]); b, _, _ = brand(self.cards[cid])
                    if s and b:
                        groups[(s, b)].append(cid)
                result = self.group_select(composition, groups, sid, p, u)
                mode = 'Набор: один поставщик и бренд'
                if result is None:
                    groups = defaultdict(list)
                    for cid in ids:
                        b, _, _ = brand(self.cards[cid])
                        if b:
                            groups[b].append(cid)
                    result = self.group_select(composition, groups, sid, p, u)
                    mode = 'Набор: один бренд, разные поставщики'
                if result is None:
                    result = self.group_select(composition, {'mixed': ids}, sid, p, u)
                    mode = 'Набор: разные поставщики и бренды'
                if result is None:
                    shortage = [{'external_code': c, 'required': float(n),
                        'available': float(sum(self.available(sid, cid, p, u) for cid in self.by_code[c]))}
                        for c, n in composition.items()
                        if sum(self.available(sid, cid, p, u) for cid in self.by_code[c]) < n]
                    return {'ready': False, 'reason': 'Недостаточно компонентов набора', 'shortage': shortage}
                selected, p, u, _ = result
            else:
                if self.available(sid, current['id'], p, u) >= qty:
                    selected = self.select(code, qty, [current['id']], sid, p, u)
                    mode = 'Без замены'
                else:
                    if not self.allow_single_replacements:
                        return {'ready': False, 'reason': 'Недостаточно исходного товара вне набора',
                                'shortage': [{'external_code': code, 'required': float(qty)}]}
                    own_supplier = supplier(current)[0]
                    ids = [cid for cid in self.by_code[code] if cid != current['id']
                           and supplier(self.cards[cid])[0] and supplier(self.cards[cid])[0] != own_supplier]
                    selected = self.select(code, qty, ids, sid, p, u)
                    mode = 'Замена поставщика по внешнему коду'
                    if selected is None:
                        return {'ready': False, 'reason': 'Нет достаточного остатка аналога у другого поставщика',
                            'shortage': [{'external_code': code, 'required': float(qty),
                                'available': float(sum(self.available(sid, cid, pool, used) for cid in ids))}]}
            lines.append({'source': row, 'selected': selected, 'mode': mode, 'is_set': is_set})
        return {'ready': True, 'lines': lines, 'pool': p, 'used': u}


def cents(value):
    return num(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def line_sum(row, order):
    value = num(row['price']) * num(row['quantity']) * (1 - num(row.get('discount')) / 100)
    if order.get('vatEnabled') and not order.get('vatIncluded') and row.get('vatEnabled', True):
        value *= 1 + num(row.get('vat')) / 100
    return cents(value)


def line_vat(row, order):
    if not order.get('vatEnabled') or not row.get('vatEnabled', True) or not row.get('vat'):
        return Decimal(0)
    vat = num(row['vat'])
    if order.get('vatIncluded'):
        return cents(line_sum(row, order) * vat / (100 + vat))
    net = cents(num(row['price']) * num(row['quantity']) * (1 - num(row.get('discount')) / 100))
    return cents(net * vat / 100)


def balance_component_tax(rows, source, order):
    target_net, target_vat = line_sum(source, order), line_vat(source, order)
    if sum(line_vat(r, order) for r in rows) == target_vat:
        return rows
    assert all(num(r['quantity']) == 1 for r in rows)
    net_base = [line_sum(r, order) for r in rows]
    vat_base = [line_vat(r, order) for r in rows]
    states = {(0, 0, 0): []}
    for i, row in enumerate(rows):
        choices = []
        for delta in sorted(range(-30, 31), key=abs):
            if num(row['price']) + delta <= 0: continue
            candidate = dict(row, price=float(num(row['price']) + delta))
            choices.append((delta, int(line_sum(candidate, order) - net_base[i]),
                            int(line_vat(candidate, order) - vat_base[i])))
        next_states = {}
        for (price_delta, net_delta, vat_delta), values in states.items():
            for dp, dn, dv in choices:
                key = price_delta + dp, net_delta + dn, vat_delta + dv
                next_states.setdefault(key, values + [dp])
        states = next_states
    values = states.get((0, int(target_net - sum(net_base)), int(target_vat - sum(vat_base))))
    if values is None:
        raise ValueError('Требуется ручное распределение НДС до копейки')
    result = [dict(row, price=float(num(row['price']) + delta)) for row, delta in zip(rows, values)]
    assert sum(line_sum(r, order) for r in result) == target_net
    assert sum(line_vat(r, order) for r in result) == target_vat
    assert sum(num(r['price']) for r in result) == sum(num(r['price']) for r in rows)
    return result


def expanded_positions(line, order, cards):
    source = line['source']
    fields = ('quantity', 'price', 'discount', 'vat', 'vatEnabled', 'reserve')
    template = {k: source[k] for k in fields if k in source}
    if not line['is_set']:
        rows = []
        for item in line['selected']:
            row = dict(template, quantity=item['quantity'], assortment={'meta': cards[item['id']]['meta']})
            row['reserve'] = float(num(item['quantity']) * min(1, num(source.get('reserve')) / num(source['quantity'])))
            if line['mode'] == 'Без замены':
                row['id'] = source['id']
            rows.append(row)
        return rows
    # All observed set order quantities are 1. Split the existing price, never reprice it.
    assert num(source['quantity']) == 1
    units = [item['id'] for item in line['selected'] for _ in range(int(item['quantity']))]
    assert len(units) == sum(num(x['quantity']) for x in line['selected'])
    assert num(source['price']) == cents(source['price'])
    total = int(source['price']); base, remainder = divmod(total, len(units))
    prices = [base + (i < remainder) for i in range(len(units))]
    target = line_sum(source, order)

    def amount(values):
        return sum(line_sum(dict(template, quantity=1, price=price), order) for price in values)

    if amount(prices) != target:
        found = None
        for i in range(len(prices)):
            for j in range(i + 1, len(prices)):
                for delta in range(-20, 21):
                    trial = list(prices); trial[i] += delta; trial[j] -= delta
                    if min(trial) > 0 and amount(trial) == target:
                        found = trial; break
                if found is not None: break
            if found is not None: break
        if found is None:
            # More than one pair may need adjustment: four equally priced units
            # can each round up, while the original set rounded down once.
            states = {(0, 0): []}
            baseline = [line_sum(dict(template, quantity=1, price=x), order) for x in prices]
            target_delta = int(target - sum(baseline))
            for i, price in enumerate(prices):
                choices = [(delta, int(line_sum(dict(template, quantity=1, price=price + delta), order) - baseline[i]))
                           for delta in range(-20, 21) if price + delta > 0]
                next_states = {}
                for (total_delta, net_delta), deltas in states.items():
                    for delta, net in choices:
                        key = total_delta + delta, net_delta + net
                        next_states.setdefault(key, deltas + [delta])
                states = next_states
            deltas = states.get((0, target_delta))
            if deltas is not None:
                found = [p + d for p, d in zip(prices, deltas)]
        if found is None:
            raise ValueError('Требуется ручное распределение скидки набора до копейки')
        prices = found
    rows = []
    for cid, price in zip(units, prices):
        row = dict(template, quantity=1.0, price=price, assortment={'meta': cards[cid]['meta']})
        row['reserve'] = float(min(1, num(source.get('reserve'))))
        rows.append(row)
    assert sum(line_sum(r, order) for r in rows) == target
    assert sum(num(r['price']) for r in rows) == num(source['price'])
    return balance_component_tax(rows, source, order)


def build():
    import pandas as pd
    cards, stock, boms = load('candidates.json'), load('stock.json'), load('boms.json')
    planner = Planner(cards, stock, boms)
    pool, used = dict(planner.initial), {}
    state = next(s for s in load('metadata.json')['states'] if s['name'] == 'Собирается')
    report, changes, payloads, details, components = [], [], [], [], []
    for order in orders():
        result = planner.order(order, pool, used)
        individual = planner.order(order, planner.initial, {})
        record = {'Заказ': order['name'], 'Дата заказа': order['moment'], 'Склад': order['store']['name'],
                  'ID заказа': order['id'], 'Ссылка МС': order['meta'].get('uuidHref', ''),
                  'Сумма заказа, ₽': order['sum'] / 100, 'Готов': result['ready'],
                  'Проверка отдельно': individual['ready'], 'Причина': result.get('reason', ''),
                  'План. дата отгрузки': '22.10.2026', 'Плановый статус': 'Собирается',
                  'Порядок обработки': len(report) + 1}
        record['Конфликт общего остатка'] = not result['ready'] and individual['ready']
        if record['Конфликт общего остатка']:
            record['Причина'] = 'Остаток уже распределён более ранним заказам'
        if result['ready']:
            new_positions = []
            try:
                for line in result['lines']:
                    new_positions += expanded_positions(line, order, cards)
                before = sum(line_sum(r, order) for r in order['positions']['rows'])
                after = sum(line_sum(r, order) for r in new_positions)
                assert before == after
            except (AssertionError, ValueError) as exc:
                result = {'ready': False, 'reason': 'Распределение суммы требует проверки: ' + str(exc)}
                record.update({'Готов': False, 'Причина': result['reason']})
            else:
                pool, used = result.pop('pool'), result.pop('used')
                modes = sorted({line['mode'] for line in result['lines']})
                record['Действие'] = '; '.join(modes)
                record['Набор'] = any(line['is_set'] for line in result['lines'])
                record['Сумма позиций сохранена'] = before == after
                changed = any(line['mode'] != 'Без замены' for line in result['lines'])
                payload = {'state': {'meta': state['meta']}, 'deliveryPlannedMoment': TARGET_DATE}
                if changed:
                    payload['positions'] = new_positions
                payloads.append({'order_id': order['id'], 'order_name': order['name'],
                    'precondition': {'state': 'Ожидает подтверждения', 'updated': order['updated'],
                                     'store_id': order['store']['id'], 'sum': order['sum']},
                    'requires_fresh_stock_and_order_check': True, 'payload': payload})
                for line in result['lines']:
                    for item in line['selected']:
                        card = cards[item['id']]; key = order['store']['id'] + '/' + item['id']
                        brand_key, brand_name, brand_source = brand(card)
                        row = {'Заказ': order['name'], 'Склад': order['store']['name'], 'Действие': line['mode'],
                            'Исходный внешний код': line['source']['assortment']['externalCode'],
                            'Исходный товар': line['source']['assortment']['name'],
                            'Выбранный внешний код': item['external_code'], 'Код МС': card.get('code'),
                            'Артикул': card.get('article'), 'Выбранный товар': card['name'],
                            'Количество': item['quantity'], 'Поставщик': supplier(card)[1],
                            'Группа поставщика': supplier(card)[0], 'Бренд': brand_name, 'Источник бренда': brand_source,
                            'Остаток склада': stock.get(key, {}).get('stock', 0),
                            'Резерв склада': stock.get(key, {}).get('reserve', 0), 'ID товара': item['id']}
                        details.append(row)
                        if line['mode'] != 'Без замены': changes.append(row)
        if not result['ready']:
            record['Действие'] = 'Не подготовлен к сборке'
            record['Дефицит'] = json.dumps(result.get('shortage', []), ensure_ascii=False)
        report.append(record)
    for parent, composition in boms.items():
        if isinstance(tc(parent), list):
            components += [{'Код набора': parent, 'Код компонента': code, 'Количество в наборе': qty,
                            'Источник': 'ТК /api/catalog/mix_data, свежая сверка'} for code, qty in composition.items()]
    allocations = []
    for key, qty in used.items():
        if qty <= 0: continue
        assert qty <= num(stock.get(key, {}).get('stock'))
        sid, cid = key.split('/')
        allocations.append({'Склад ID': sid, 'Код': cards[cid].get('code'), 'Внешний код': cards[cid]['externalCode'],
            'Товар': cards[cid]['name'], 'Остаток': stock[key]['stock'], 'В плане': float(qty),
            'Остаток после плана': float(num(stock[key]['stock']) - qty), 'Свободно после плана с резервами': float(pool[key])})
    save('prepared_updates.json', payloads)
    save('order_report.json', report)
    save('selected_positions.json', details)
    save('allocation.json', allocations)
    summary = {'orders_total': len(report), 'ready': sum(r['Готов'] for r in report),
        'ready_without_replacements': sum(r['Готов'] and r['Действие'] == 'Без замены' for r in report),
        'ready_with_replacements': sum(r['Готов'] and r['Действие'] != 'Без замены' for r in report),
        'ready_bundle_orders': sum(r['Готов'] and r.get('Набор', False) for r in report),
        'individually_ready': sum(r['Проверка отдельно'] for r in report),
        'shared_stock_conflicts': sum(r['Конфликт общего остатка'] for r in report),
        'not_ready': sum(not r['Готов'] for r in report), 'ms_write_requests': 0,
        'planned_date': '2026-10-22', 'priority': 'Дата заказа по возрастанию',
        'stock_policy': 'Физический stock минус reserve, без inTransit; свой резерв возвращён только внутри проверяемого заказа; общий расход по списку ограничен stock.',
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'requires_pre_apply_recheck': True, 'action_counts': dict(Counter(r['Действие'] for r in report))}
    save('summary.json', summary)
    with pd.ExcelWriter(OUT / 'План_заказов_МС_22.10.2026.xlsx', engine='openpyxl') as writer:
        notes = [
            ('Режим', 'Только подготовка: в МойСкладе ничего не изменено'),
            ('Всего заказов / готовы / не готовы', f"{len(report)} / {summary['ready']} / {summary['not_ready']}"),
            ('Подготовленное действие', 'Статус Собирается; плановая дата отгрузки 22.10.2026'),
            ('Склады', 'Склад каждого заказа сохранён, между складами товар не переносится'),
            ('Наборы', 'Состав подтверждён непосредственно в ТК; количества повторяющихся кодов учтены'),
            ('Приоритет комплектов', 'Полный комплект одного поставщика и бренда; затем одного бренда; затем смешанный'),
            ('Доступный остаток', summary['stock_policy']),
            ('Цены и скидки', 'Исходные суммы заказов, скидки и НДС сохраняются; цена набора распределена между компонентами'),
            ('Порядок', 'По дате заказа, начиная с самого раннего; один остаток не выделяется повторно'),
            ('Перед применением', 'Повторно проверить статус, версию заказа и остатки: этот файл сам ничего не резервирует'),
            ('Подготовлено UTC', summary['captured_at']),
        ]
        pd.DataFrame(notes, columns=['Параметр', 'Значение']).to_excel(writer, sheet_name='Условия плана', index=False)
        pd.DataFrame(report).to_excel(writer, sheet_name='Все заказы', index=False)
        pd.DataFrame([r for r in report if r['Готов']]).to_excel(writer, sheet_name='Готовы к сборке', index=False)
        pd.DataFrame([r for r in report if not r['Готов']]).to_excel(writer, sheet_name='Не собраны', index=False)
        pd.DataFrame(changes).to_excel(writer, sheet_name='Замены товаров', index=False)
        pd.DataFrame(details).to_excel(writer, sheet_name='Все выбранные позиции', index=False)
        pd.DataFrame(components).to_excel(writer, sheet_name='Составы наборов ТК', index=False)
        pd.DataFrame(allocations).to_excel(writer, sheet_name='Распределение остатков', index=False)
        for sheet in writer.book:
            sheet.freeze_panes = 'A2'; sheet.auto_filter.ref = sheet.dimensions
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith('https://online.moysklad.ru/'):
                        cell.hyperlink = cell.value; cell.style = 'Hyperlink'
            for column in sheet.columns:
                letter = column[0].column_letter
                sheet.column_dimensions[letter].width = min(65, max(14, max(len(str(c.value or '')) for c in column[:100]) + 2))
    pd.DataFrame(report).to_csv(OUT / 'orders.csv', sep=';', index=False, encoding='utf-8-sig')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    import sys
    build() if '--build' in sys.argv else collect()
