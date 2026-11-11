# поток: mkt / ozon
"""Compare current nonarchived stocked SKU cards; no marketplace or DB writes."""
import collections
import csv
import html
import json
import re
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, '/opt/mp-analytics')
from collectors.launch_cards import classify

O = Path(__file__).parent
read = lambda p: json.loads(p.read_text())
save = lambda n, x: (O / n).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')
accounts = {}
all_rows = []
for account in ('oz_acc1', 'oz_acc2'):
    root = O / account
    acquisition = read(root / 'acquisition_summary.json')
    assert acquisition['account'] == account and acquisition['scope'] == 'SELLER_CATALOG_ONLY'
    pids = set(map(str, read(root / 'product_ids_all.json')))
    items = {}
    for path in sorted((root / 'raw').glob('info_all_*.json')):
        raw = read(path)
        assert raw['account'] == account
        if raw['http'] != 200:
            continue
        assert set(map(str, raw['body']['product_id'])) == {str(x['id']) for x in raw['response']['items']}
        for item in raw['response']['items']:
            assert str(item['id']) not in items
            items[str(item['id'])] = (item, str(path.relative_to(O)), raw['at_utc'])
    assert set(items) == pids
    rows = []
    count = collections.Counter()
    ready_count = collections.Counter()
    quantities = collections.Counter()
    catalog_types = collections.Counter()
    exclusions = collections.Counter()
    for pid, (item, source, observed) in items.items():
        offer = item['offer_id']
        role = classify(offer, item['name'], account)
        quantity = role[2] if role and role[1] == 'bundle' else None
        if quantity:
            group = 'bundle'
        elif (re.fullmatch(r'[0-9]{5,}', offer) if account == 'oz_acc1' else re.match(r'^[0-9]{5}', offer)):
            group = 'child'
        else:
            group = 'other'
        if item.get('is_archived') or item.get('is_autoarchived'):
            exclusions['archived_or_autoarchived'] += 1
            continue
        catalog_types[group] += 1
        stocks = item.get('stocks') or {}
        available = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stocks.get('stocks') or []), D(0))
        if stocks.get('has_stock') is not True or available <= 0:
            exclusions['no_free_stock'] += 1
            continue
        sku = str(item.get('sku') or next((s['sku'] for s in item.get('sources') or [] if s.get('sku')), ''))
        assert sku, 'Stocked card without identifiable SKU'
        ready = any(a.get('availability') == 'AVAILABLE' for a in item.get('availabilities') or []) and not item.get('errors') and D(str(item.get('price') or 0)) > 0 and item.get('currency_code') == 'RUB' and (item.get('visibility_details') or {}).get('has_price') is not False
        row = {'account': account, 'sku': sku, 'product_id': pid, 'offer_id': offer,
               'name': item['name'], 'group': group, 'bundle_multiplier': quantity or '',
               'family_external_code': offer[:4], 'available_stock_units': str(available),
               'basic_channel_price_errors_checks_passed': ready,
               'model_id': str((item.get('model_info') or {}).get('model_id') or ''),
               'metadata_observed_at_utc': observed, 'source': source,
               'scope_includes_advertised_and_unadvertised': True}
        rows.append(row)
        count[group] += 1
        ready_count[group] += bool(ready)
        if quantity:
            quantities[quantity] += 1
    for field in ['sku', 'product_id', 'offer_id']:
        assert len({r[field] for r in rows}) == len(rows)
    assert sum(count.values()) == len(rows)
    all_rows += rows
    accounts[account] = {'snapshot_started_at_utc': acquisition['started_at_utc'],
                         'snapshot_finished_at_utc': acquisition['finished_at_utc'],
                         'listed_cards': len(pids), 'covered_cards': len(items),
                         'all_nonarchived_instock_SKUs': len(rows), 'instock_types': dict(count),
                         'basic_checks_passed_by_type': dict(ready_count),
                         'all_nonarchived_catalog_types': dict(catalog_types),
                         'bundle_quantities_instock': dict(quantities), 'exclusions': dict(exclusions),
                         'child_families_instock': len({r['family_external_code'] for r in rows if r['group'] == 'child'}),
                         'metadata_coverage_complete': True, 'primary_SKUs_unique': True}

def csvout(name, rows):
    assert rows
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

csvout('all_instock_nonarchived.csv', all_rows)
for account in accounts:
    for group in ['bundle', 'child']:
        csvout(f'{account}_{group}.csv', [r for r in all_rows if r['account'] == account and r['group'] == group])
comparison = []
def add(label, n1, n2):
    comparison.append({'group': label, 'oz_acc1_SKUs': n1, 'oz_acc2_SKUs': n2, 'acc2_minus_acc1': n2 - n1})
for group, label in [('bundle', 'Бандлы всего'), ('child', 'Дочерние')]:
    add(label, accounts['oz_acc1']['instock_types'].get(group, 0), accounts['oz_acc2']['instock_types'].get(group, 0))
for quantity in sorted({int(q) for a in accounts.values() for q in a['bundle_quantities_instock']}):
    add(f'Бандлы x{quantity}', accounts['oz_acc1']['bundle_quantities_instock'].get(quantity, 0), accounts['oz_acc2']['bundle_quantities_instock'].get(quantity, 0))
csvout('comparison.csv', comparison)
result = {'scope': 'all nonarchived in-stock SKU cards including both advertised and unadvertised',
          'stock_rule': 'has_stock true AND sum(max(present-reserved,0))>0',
          'accounts': accounts, 'comparison': comparison, 'new_advertising_reads': 0,
          'marketplace_configuration_writes': 0, 'production_DB_writes': 0}
save('comparison_summary.json', result)
slug = '/api/wb-bids/reports/ozon-accounts-inventory-2026-11-08'
body = '<h1>Ozon · акк1 и акк2 · бандлы и дочерние</h1><p>Сравнение всех неархивных SKU в наличии, включая товары в рекламе и вне рекламы. Считаются карточки SKU, не физические единицы внутри бандла.</p>'
body += '<table><tr><th>Группа</th><th>Акк1</th><th>Акк2</th><th>Акк2 минус акк1</th></tr>'
for row in comparison:
    body += '<tr>' + ''.join('<td>'+html.escape(str(row[k]))+'</td>' for k in ['group', 'oz_acc1_SKUs', 'oz_acc2_SKUs', 'acc2_minus_acc1']) + '</tr>'
body += '</table><p>Строки кратности раскрывают «Бандлы всего»; повторно их не складывать с итогом.</p>'
body += '<p><a href="/api/wb-bids/reports/ozon-child-gap-2026-11-08">Почему дочерних на акк2 меньше: отдельная сверка с актуальным архивом и соответствиями принтеров</a>. Неархивная таблица выше не включает дочерние, переведённые в автоархив.</p>'
for account, a in accounts.items():
    body += f'<p>{account}: чтение {a["snapshot_started_at_utc"]} — {a["snapshot_finished_at_utc"]} UTC. Покрытие карточек {a["covered_cards"]}/{a["listed_cards"]}. Всего неархивных SKU в наличии {a["all_nonarchived_instock_SKUs"]}. <a href="{slug}-{account}-bundle">Бандлы CSV</a>; <a href="{slug}-{account}-child">Дочерние CSV</a>.</p>'
body += '<p>Одинаковый фильтр наличия: Seller has_stock=true и положительная сумма свободного остатка present-reserved. Архивные и автоархивные исключены. Остаток родителя не требуется. Канал/цена/ошибки сохранены отдельными полями, не меняют базовый фильтр наличия.</p>'
body += '<p>Бандлы определяются по явной кратности артикула; для случайного хвоста акк2 кратность подтверждается названием. Дочерние акк1 — цифровой артикул длиннее4цифр; акк2 —4цифры внешнего кода, затем номер дочерней и случайный хвост по уточнению владельца. model_id не используется вместо этой связи.</p>'
body += f'<p><a href="{slug}-table">Сравнение CSV</a>. Новых рекламных выгрузок0, изменений рекламы0, записей в производственную БД0.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сравнение ассортимента Ozon</title><style>body{font:16px/1.5 system-ui;max-width:1000px;margin:25px auto;padding:0 16px;color:#172239}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:10px;text-align:left}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O / 'RESULT.md').write_text('# Сравнение акк1/акк2: все неархивные в наличии\n\n'+'\n'.join(f"{r['group']}: акк1 {r['oz_acc1_SKUs']}, акк2 {r['oz_acc2_SKUs']}, разница {r['acc2_minus_acc1']}" for r in comparison)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps(result, ensure_ascii=False))
