# поток: mkt / ozon
"""Offline acc2 inventory partition using the existing project card classifier."""
import collections
import csv
import datetime as dt
import html
import itertools
import json
import sys
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
from collectors.launch_cards import classify, normalized_title

O = Path(__file__).parent
R = O / 'raw'
read = lambda p: json.loads(Path(p).read_text())
save = lambda name, x: (O / name).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')
asof = dt.datetime.fromisoformat(read(O / 'acquisition_summary.json')['finished_at_utc'])
TZ = ZoneInfo('Europe/Moscow')
cutoff = dt.datetime(2026, 5, 8, tzinfo=TZ)
meta = read(O / 'current_campaigns.json')
assert meta['account'] == 'oz_acc2'
global_cpo = [str(c['id']) for c in meta['campaigns'] if c['advObjectType'] == 'ALL_SKU_PROMO' and c['state'] == 'CAMPAIGN_STATE_RUNNING']
assert global_cpo == ['23608084']
ids = set(map(str, read(O / 'product_ids_all.json')))
items, provenance = {}, {}
for p in sorted(R.glob('info_all_*.json')):
    x = read(p)
    assert x['account'] == 'oz_acc2' and x['http'] == 200
    requested = set(map(str, x['body']['product_id']))
    returned = {str(r['id']) for r in x['response']['items']}
    assert requested == returned
    for r in x['response']['items']:
        pid = str(r['id'])
        assert pid not in items
        items[pid] = r
        provenance[pid] = str(p.relative_to(O))
assert set(items) == ids
alias = {}
for pid, r in items.items():
    for sku in [r.get('sku')] + [s.get('sku') for s in r.get('sources') or []]:
        if sku:
            assert str(sku) not in alias or alias[str(sku)] == pid
            alias[str(sku)] = pid
cpc = collections.defaultdict(set)
for p in O.glob('active_products_*.json'):
    x = read(p)
    assert x['account'] == 'oz_acc2' and x['http'] == 200
    for r in x['products']:
        cpc[alias.get(str(r['sku']), 'UNKNOWN:' + str(r['sku']))].add(str(x['campaign_id']))
promo = read(O / 'current_search_promo.json')
assert promo['account'] == 'oz_acc2'
promo_enabled = set()
for r in promo['products']:
    if r.get('searchPromoStatus') is True:
        for sku in [r.get('sku'), r.get('sourceSku')]:
            if sku:
                promo_enabled.add(alias.get(str(sku), 'UNKNOWN:' + str(sku)))
role_snapshot = read(O / 'existing_role_map.json')
assert role_snapshot['account'] == 'oz_acc2'
saved_roles = {r['offer']: r for r in role_snapshot['rows']}
rows = []
stocked = []
for pid, r in items.items():
    archived = bool(r.get('is_archived') or r.get('is_autoarchived'))
    stock = r.get('stocks') or {}
    available = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stock.get('stocks') or []), D(0))
    if archived or stock.get('has_stock') is not True or available <= 0:
        continue
    own_sku = r.get('sku') or next((s.get('sku') for s in r.get('sources') or [] if s.get('sku')), '')
    offer, name = r['offer_id'], r['name']
    saved = saved_roles.get(offer)
    hints = {}
    if saved and saved['role'] != 'unknown' and normalized_title(saved['name']) == normalized_title(name):
        hints[offer] = (saved['role'], saved['multiplier'], saved['role_basis'])
    role = classify(offer, name, 'oz_acc2', hints)
    ext, kind, multiplier, basis = role or ('', 'unknown', None, 'нет подтверждённого типа')
    created = dt.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')) if r.get('created_at') else None
    young = cutoff <= created <= asof if created else None
    # Unknown role remains separate: do not disguise possible printer cards as old/new main cards.
    segment = 'bundles' if kind == 'bundle' else 'children' if kind == 'printer' else 'unknown_type' if kind == 'unknown' else 'unknown_date' if created is None else 'new_6months' if young else 'old'
    failures = []
    if not own_sku:
        failures.append('missing_SKU')
    if not any(a.get('availability') == 'AVAILABLE' for a in r.get('availabilities') or []):
        failures.append('no_available_channel')
    if r.get('errors'):
        failures.append('card_errors')
    if D(str(r.get('price') or 0)) <= 0 or r.get('currency_code') != 'RUB':
        failures.append('no_positive_RUB_price')
    if (r.get('visibility_details') or {}).get('has_price') is False:
        failures.append('price_not_visible')
    item = {'account': 'oz_acc2', 'sku': str(own_sku), 'product_id': pid, 'offer_id': offer, 'name': name,
            'segment': segment, 'role': kind, 'role_basis': basis, 'role_source': 'collectors.launch_cards.classify + saved prc_launch_card exact-title hints',
            'saved_role_source_checked_at': saved['checked_at'] if saved else '', 'article_family_prefix': ext,
            'model_id': str((r.get('model_info') or {}).get('model_id') or ''), 'bundle_multiplier': multiplier or '',
            'available_stock': str(available), 'created_at_utc': r.get('created_at', ''),
            'created_within_6months': young, 'outside_running_CPC': not bool(cpc[pid]),
            'running_CPC_campaign_ids': ';'.join(sorted(cpc[pid])), 'selected_CPO_enabled': pid in promo_enabled,
            'global_CPO_running': True, 'completely_outside_all_advertising_confirmed': False,
            'own_ready_basic_checks': not failures, 'own_readiness_failures': ';'.join(failures),
            'source': provenance[pid], 'automatic_application_allowed': False}
    stocked.append(item)
    if not cpc[pid] and own_sku:
        rows.append(item)

fields = list(stocked[0])
def output(name, values):
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(values)

groups = ['bundles', 'children', 'new_6months', 'old', 'unknown_type', 'unknown_date']
parts = {g: [r for r in rows if r['segment'] == g] for g in groups}
assert sum(map(len, parts.values())) == len(rows)
for key in ['sku', 'product_id', 'offer_id']:
    assert len({r[key] for r in rows}) == len(rows)
    assert all(not ({r[key] for r in parts[a]} & {r[key] for r in parts[b]}) for a, b in itertools.combinations(groups, 2))
output('all_in_stock_nonarchived.csv', stocked)
output('all_outside_cpc_in_stock.csv', rows)
output('outside_CPC_and_selected_CPO.csv', [r for r in rows if not r['selected_CPO_enabled']])
for g, rs in parts.items():
    output(g + '.csv', rs)
quantities = collections.Counter(r['bundle_multiplier'] for r in parts['bundles'])
for q in sorted(quantities):
    output(f'bundles_x{q}.csv', [r for r in parts['bundles'] if r['bundle_multiplier'] == q])
summary = {'account': 'oz_acc2', 'asof_utc': asof.isoformat(), 'asof_msk': asof.astimezone(TZ).isoformat(),
           'catalog_nonarchived_requested_cards': len(ids), 'catalog_returned_cards': len(items),
           'nonarchived_instock_cards': len(stocked), 'instock_missing_SKU': sum(not r['sku'] for r in stocked),
           'instock_in_running_CPC': sum(not r['outside_running_CPC'] for r in stocked),
           'instock_outside_running_CPC': len(rows), 'counts': {g: len(rs) for g, rs in parts.items()},
           'bundle_quantities': dict(quantities), 'outside_CPC_selected_CPO_enabled': sum(r['selected_CPO_enabled'] for r in rows),
           'outside_CPC_and_selected_CPO': sum(not r['selected_CPO_enabled'] for r in rows),
           'ready_basic_checks_outside_CPC': sum(r['own_ready_basic_checks'] for r in rows),
           'ready_basic_checks_by_group': {g: sum(r['own_ready_basic_checks'] for r in rs) for g, rs in parts.items()},
           'new_window_from_msk': cutoff.isoformat(), 'new_window_through_utc': asof.isoformat(),
           'all_group_intersections_zero': True, 'global_CPO_campaigns_running': global_cpo,
           'completely_no_advertising_count_verified': False, 'role_unknown_count': len(parts['unknown_type']),
           'unknown_type_created_in_6months': sum(r['created_within_6months'] is True for r in parts['unknown_type']),
           'no_relative_stock_requirement': True, 'marketplace_configuration_writes': 0, 'production_DB_writes': 0}
save('inventory_summary.json', summary)

expense = read(O / 'bonus_lifetime_expense.json')
assert expense['account'] == 'oz_acc2' and expense['http'] == 200
funding = collections.defaultdict(lambda: [D(0), D(0), D(0)])
for r in expense['response']['rows']:
    assert '2026-09-14' <= r['date'] <= '2026-11-08'
    for i, k in enumerate(['moneySpent', 'bonusSpent', 'prepaymentSpent']):
        funding[str(r['id'])][i] += D(r[k].replace(',', '.').replace(' ', ''))
bonus = []
for c in meta['campaigns']:
    if c.get('isAutocreated') and 'новинок' in c['title']:
        cid = str(c['id'])
        bonus.append({'account': 'oz_acc2', 'campaign_id': cid, 'state': c['state'],
                      'fromDate': c['fromDate'], 'strategy': c['productAutopilotStrategy'],
                      'campaign_budget_field_rub_NOT_BONUS_BALANCE': str(D(c['budget']) / 1000000),
                      'bonus_spent_since_20260914': str(funding[cid][1]),
                      'money_spent_since_20260914': str(funding[cid][0]),
                      'available_bonus_balance': None, 'balance_verified': False})
save('bonus_status.json', {'account': 'oz_acc2', 'observed_at_utc': expense['at_utc'], 'available_bonus_balance_verified': False,
                         'balance_reads': 'GET /api/client/balance and /api/client/finance/balance returned404; no supported balance read confirmed',
                         'do_not_derive_available_bonus_from_campaign_budget': True, 'campaigns': bonus,
                         'official_terms_url': 'https://ir.ozone.ru/s3/adv/new_sku_pt3.pdf',
                         'bonus_applies_only_to_Ozon_selected_SKUs': True,
                         'manual_added_SKUs_not_confirmed_bonus_eligible': True,
                         'changes_may_continue_promotion_in_rubles_after_bonus': True})
labels = {'bundles': 'Бандлы', 'children': 'Дочерние по действующей карте проекта', 'new_6months': 'Новинки за полгода',
          'old': 'Старые основные товары', 'unknown_type': 'Тип карточки не установлен', 'unknown_date': 'Неизвестна дата создания'}
esc = lambda x: html.escape(str(x))
slug = '/api/wb-bids/reports/ozon-acc2-inventory-bonus-2026-11-08'
body = '<h1>Акк2 · бонусы и товары вне CPC</h1><p>Срез '+esc(summary['asof_msk'])+'. Только чтение. Неархивные товары с has_stock=true и положительным свободным остатком.</p>'
body += '<h2>Бонусы</h2><p><b>Остаток доступных бонусов не подтверждён.</b> API возвращает расходы и лимит бюджета кампании, но подтверждённого чтения бонусного баланса нет. Разность лимита и расходов не считается остатком бонусов.</p>'
body += '<p>Работает бонусная РК44499213, 2 SKU, стратегия MAX_CLICKS. С начала её работы 05.10 по текущий срез08.11 списано 2 669,42 бонуса, денег0. Бюджетное поле10 000 ₽ не означает10 000 доступных бонусов. РК49322606 остановлена, остальные пять бонусных РК неактивны. Истечение/сгорание бонусов через API не подтверждено.</p>'
body += '<p><a href="https://ir.ozone.ru/s3/adv/new_sku_pt3.pdf">Официальные условия, п.3.1–3.2 и3.9</a>: бонусы закреплены только за выбранными Ozon товарами, действуют до60дней. Добавление SKU не даёт ему право на бонусы. Состав менять технически допускается; по нынешней стратегии ставки выбирает Ozon. Для ручной средней цены клика требуется другая стратегия. Любое вмешательство, включая остановку с повторным запуском, может перевести дальнейшее продвижение на рубли после исчерпания бонусов.</p>'
body += '<h2>Товары вне активной оплаты за клик</h2><p>Всего в наличии '+str(len(stocked))+' неархивных карточек, из них '+str(len(rows))+' SKU вне работающих CPC-кампаний. На аккаунте включена CPO «Все товары», РК23608084: эти списки нельзя называть полностью вне рекламы. Из списка у '+str(summary['outside_CPC_selected_CPO_enabled'])+' включена также индивидуальная CPO. Отсутствие индивидуальной CPO не исключает глобальную.</p>'
body += '<table><tr><th>Группа</th><th>В наличии вне CPC</th><th>Дополнительно прошли базовые проверки канала/цены/ошибок</th><th>Список</th></tr>'
for g in groups:
    body += f'<tr><td>{esc(labels[g])}</td><td>{len(parts[g])}</td><td>{summary["ready_basic_checks_by_group"][g]}</td><td><a href="{slug}-{g}">CSV</a></td></tr>'
body += f'<tr><th>Всего</th><th>{len(rows)}</th><th>{summary["ready_basic_checks_outside_CPC"]}</th><td><a href="{slug}-all">Все SKU · CSV</a></td></tr></table>'
body += '<p>Бандлы по кратности: '+', '.join(f'<a href="{slug}-x{k}">x{k} — {v}</a>' for k,v in sorted(quantities.items()))+'. Все кратности сохранены: ассортимент акк2 не ограничивается x2/x6/x10.</p>'
body += '<p>Группы не пересекаются. Приоритет: подтверждённый бандл → дочерняя карточка по действующему классификатору → основная новинка → основная старая. Неизвестный тип оставлен отдельно, чтобы возможных дочерних не скрыть внутри новинок/старых. Карточки с неизвестной датой также отдельно.</p>'
body += '<p>Новинки созданы с08.05.2026 по текущий срез; источник Seller created_at. Бандлы/дочерние в новинки не дублируются. Молодые карточки неизвестного типа: '+str(summary['unknown_type_created_in_6months'])+'.</p>'
body += '<h2>Почему нужна проверка типов</h2><p>Все19 659 текущих offer_id имеют длину12. На Дисквэре используется случайный хвост: 0898BJH5U125 не считается дочерним только по длине. Окончание X2 в случайном хвосте также не доказывает бандл. Использован существующий collectors.launch_cards.classify и сохранённая карта prc_launch_card акк2; кратность сверяется с названием. Основание роли и время карты есть в каждой строке CSV. Это классификация проекта, а не восстановление отсутствующего исходного цифрового артикула.</p>'
body += '<p>Наличие родителя и его остаток не являются условием включения дочки. Ozon model_id и семья по первым четырём цифрам — разные поля. Неизвестные типы требуют исходной карты артикулов/ручной проверки; распределение по группам не додумывалось.</p>'
body += '<h2>Следующий шаг</h2><p>Остаток и срок бонусов нужно подтвердить в личном кабинете, Продвижение → Оплата за клик → Бонусы. Для полной четырёхгрупповой разбивки нужен источник типов у строк «Тип не установлен». Готовые списки не являются разрешением на запуск или изменение рекламы.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Акк2: бонусы и ассортимент</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:25px auto;padding:0 16px;color:#172239}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O / 'RESULT.md').write_text('# Акк2: бонусы и ассортимент, 08.11.2026\n\nОстаток бонусов НЕ подтверждён. Бюджет РК не баланс бонусов.\n\n'+json.dumps(summary, ensure_ascii=False, indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps(summary, ensure_ascii=False))
