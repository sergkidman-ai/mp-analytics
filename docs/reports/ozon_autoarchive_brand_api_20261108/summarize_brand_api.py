# поток: mkt / ozon
"""Offline brand evidence and read-only assessment of unarchive API availability."""
import collections
import csv
import datetime as dt
import html
import json
import re
from pathlib import Path

O = Path(__file__).parent
BASE = O.parent
read = lambda p: json.loads(p.read_text())
save = lambda n, x: (O / n).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')
metadata = {}
with (BASE / 'ozon_autoarchive_dates_20261108/acc1_autoarchived_all.csv').open(encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        metadata[('oz_acc1', r['product_id'])] = {'offer': r['offer_id'], 'child': False,
            'positive_stock': r['current_positive_stock'] == 'True', 'stock': r['current_free_stock']}
for p in (BASE / 'ozon_child_gap_audit_20261108/acc2_archive/raw').glob('info_archived_*.json'):
    z = read(p)
    if z['http'] != 200:
        continue
    for r in z['response']['items']:
        if not r.get('is_autoarchived'):
            continue
        stocks = r.get('stocks') or {}
        amount = sum(max(float(s.get('present') or 0) - float(s.get('reserved') or 0), 0) for s in stocks.get('stocks') or [])
        metadata[('oz_acc2', str(r['id']))] = {'offer': r['offer_id'], 'child': bool(re.match(r'^[0-9]{5}', r['offer_id'])),
            'positive_stock': stocks.get('has_stock') is True and amount > 0, 'stock': str(amount)}

rows, seen = [], set()
for p in sorted((O / 'raw').glob('*_brand_attrs_*.json')):
    z = read(p); assert z['http'] == 200
    account = z['account']
    assert z['request']['filter']['visibility'] == 'ARCHIVED'
    requested = set(map(str, z['request']['filter']['product_id']))
    assert requested == {str(r['id']) for r in z['response']['result']}
    for r in z['response']['result']:
        key = account, str(r['id']); assert key not in seen; seen.add(key)
        m = metadata[key]; assert m['offer'] == r['offer_id']
        brand = [v for a in r['attributes'] if a['id'] == 85 for v in a.get('values') or []]
        assert len(brand) == 1 and brand[0].get('value'), 'Brand coverage must be explicit'
        rows.append({'account': account, 'product_id': key[1], 'sku': str(r.get('sku') or ''),
                     'offer_id': r['offer_id'], 'name': r['name'], 'is_autoarchived_at_inventory_snapshot': True,
                     'is_child': m['child'], 'brand_attribute_id': 85, 'brand': brand[0]['value'],
                     'brand_dictionary_value_id': brand[0].get('dictionary_value_id', ''),
                     'positive_stock_at_inventory_snapshot': m['positive_stock'],
                     'free_stock_at_inventory_snapshot': m['stock'],
                     'brand_observed_at_utc': z['at_utc'], 'brand_source': str(p.relative_to(O)),
                     'automatic_application_allowed': False})
assert seen == set(metadata)
roles = read(O / 'raw/acc2_role_permissions.json')
assert roles['account'] == 'oz_acc2' and roles['http'] == 200
allowed = {m for r in roles['response']['roles'] for m in r['methods']}
assert '/v1/product/unarchive' in allowed
limits = read(O / 'raw/acc2_product_limits.json')
assert limits['account'] == 'oz_acc2' and limits['http'] == 200
summary = {'groups': [], 'unknown_brand': 0, 'brand_source': 'live Seller attribute85 Бренд',
           'accounts_fully_covered': {'oz_acc2': 4492, 'oz_acc1': 32},
           'unarchive_method': 'POST /v1/product/unarchive', 'request_identifier': 'product_id, not SKU or offer_id',
           'acc2_current_key_explicitly_allows_method': True, 'permission_observed_at_utc': roles['at_utc'],
           'unarchive_write_test_performed': False, 'live_restore_success_verified': False,
           'documented_20260514_daily_autoarchive_restore_limit': 100,
           'documented_20260514_max_product_ids_per_request': 100,
           'documented_reset_moscow_time': '03:00', 'current_account_remaining_daily_quota_verified': False,
           'current_daily_business_limit_live_verified': False,
           'product_info_limit_response_has_autoarchive_remaining': False,
           'limit_evidence_url': 'https://ir.ozone.ru/s3/o4d-files/screen_001_574a2886.png',
           'limit_evidence_context_url': 'https://dev.ozon.ru/community/2069-Poluchat-Limit-na-vozvrat-iz-Arkhiva/',
           'old_2024_news_limit_10_not_used_as_current': True,
           'full_current_API_documentation_readable': False,
           'Ozon_configuration_writes': 0, 'production_DB_writes': 0}
grouped = collections.defaultdict(list)
for r in rows:
    grouped[(r['account'], r['is_child'], r['brand'])].append(r)
for (account, child, brand), rr in sorted(grouped.items()):
    summary['groups'].append({'account': account, 'autoarchived_scope': 'children' if child else 'other',
                              'brand': brand, 'SKUs': len(rr),
                              'positive_stock': sum(r['positive_stock_at_inventory_snapshot'] for r in rr)})
assert len(rows) == 4524
children = [r for r in rows if r['account'] == 'oz_acc2' and r['is_child']]
assert len(children) == 4383 and {r['brand'] for r in children} == {'ДС принт'}
assert sum(r['positive_stock_at_inventory_snapshot'] for r in children) == 3519
save('brand_api_summary.json', summary)
with (O / 'autoarchived_brand_by_SKU.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
save('unarchive_api_contract_DRAFT_NOT_EXECUTED.json', {
    'account': 'oz_acc2', 'status': 'DOCUMENTATION_PREVIEW_ONLY_NOT_AUTHORIZED',
    'method': 'POST', 'path': '/v1/product/unarchive', 'body_schema': {'product_id': 'array of integer Ozon product_id'},
    'execute_allowed': False, 'example_product_ids_are_not_supplied': True,
    'requirements_before_any_execution': ['explicit owner instruction to restore exact cards',
        'refresh exact card archive status and own stock', 'confirm account daily quota or use reviewed small first batch',
        'record result and read back is_autoarchived/is_archived; do not infer success from HTTP200 alone',
        'do not update ads, prices, stock or brand as part of restore']})
slug = '/api/wb-bids/reports/ozon-autoarchive-brand-api-2026-11-08'
body = '<h1>Ozon · бренд автоархивных и возможность API-восстановления</h1><p><b>Все4 383автоархивные дочерние акк2 имеют бренд «ДС принт».</b> Это текущее полеБренд(attribute85), а не вывод из названия. У3 519из них предыдущий срез API показывает положительный свободный остаток. «Картридж.орг» среди проверенных автоархивных не найден.</p>'
body += '<table><tr><th>Аккаунт / группа</th><th>Бренд</th><th>SKU</th><th>Из них с остатком</th></tr>'
for r in summary['groups']:
    body += '<tr><td>'+html.escape(r['account']+' / '+('дочерние' if r['autoarchived_scope']=='children' else 'прочие'))+'</td><td>'+html.escape(r['brand'])+'</td><td>'+str(r['SKUs'])+'</td><td>'+str(r['positive_stock'])+'</td></tr>'
body += '</table><p>Проверены4492/4492автоархивных акк2 и32/32акк1 точными product_id в6ограниченных чтениях атрибутов. Архивный состав и наличие — из уже сохранённых последних полных срезов; бренд прочитан заново. Новых широких каталогов и рекламных отчётов не было. Отсутствие указанного бренда относится к этим группам, а не ко всему ассортименту аккаунтов.</p>'
body += '<h2>Можно ли вывести через API</h2><p><b>Да, существует POST /v1/product/unarchive, и текущий ключ акк2 явно содержит право на этот метод.</b> Право проверено чтением POST/v1/roles. Метод использует Ozon product_id, не SKU и не offer_id. <a href="https://dev.ozon.ru/news/337-Daidzhest-obnovlenii-mai/">Официальное подтверждение доступности метода</a>.</p>'
body += '<p>В <a href="'+summary['limit_evidence_url']+'">скриншоте документации отмая2026</a>:до100идентификаторов в запросе и до100автоархивных товаров в сутки; сброс03:00МСК. Он опубликован в<a href="'+summary['limit_evidence_context_url']+'">обсуждении на Ozon for dev14.05.2026</a>. Старая статья2024с10товарами не используется как текущий лимит.</p>'
body += '<p><b>Текущий остаток суточного лимита акк2 не подтверждён.</b> POST/v4/product/info/limit выполнен успешно, но не вернул остаток восстановления из автоархива. Его operation_limits содержит общийrate30000вминуту, это не бизнес-лимит восстановления. Полная текущая страница Seller API не читается, поэтому100зафиксировано как документированное значение измая2026, не как проверенная сегодня свободная квота аккаунта.</p>'
body += '<p>Запрос product/unarchive не отправлялся, даже пустой или «пробный»: это изменение карточек, а поручение касалось проверки возможности. Успешное восстановление конкретных товаров пока не проверено. Возврат из архива не является разрешением менять бренд, цены, остатки или рекламные кампании.</p>'
body += '<p>Следующий конкретный шаг — выбрать небольшой набор из3 519дочерних с остатком и после прямого поручения проверить восстановление с чтением флагов обратно. Сейчас только подготовлен контракт операции, без исполняемых product_id и автоматического применения.</p>'
body += f'<p><a href="{slug}-skus">Все4 524проверенные карточки с брендом CSV</a> · <a href="/api/wb-bids/reports/ozon-autoarchive-dates-2026-11-08">Даты создания и автоархив обоих аккаунтов</a>. Изменений Ozon0, производственной БД0.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: бренд и API-восстановление</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:25px auto;padding:0 16px;color:#172239}td,th{padding:10px;border:1px solid #ddd;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O / 'RESULT.md').write_text('# Бренд и API-восстановление автоархивных\n\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps(summary,ensure_ascii=False))
