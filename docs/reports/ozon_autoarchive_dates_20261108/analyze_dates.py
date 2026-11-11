# поток: mkt / ozon
"""Offline creation dates and autoarchive comparison. Never infer publication dates."""
import collections
import csv
import datetime as dt
import html
import json
import re
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

O = Path(__file__).parent
ROOT = O.parent
TZ = ZoneInfo('Europe/Moscow')
read = lambda p: json.loads(p.read_text())
save = lambda n, x: (O / n).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')


def cards(account, source):
    summary = read(source / 'acquisition_summary.json')
    assert summary['account'] == account
    pids = set(map(str, read(source / 'product_ids_archived.json')))
    rows = {}
    for path in sorted((source / 'raw').glob('info_archived_*.json')):
        z = read(path)
        assert z['account'] == account
        if z['http'] != 200:
            continue
        assert set(map(str, z['body']['product_id'])) == {str(x['id']) for x in z['response']['items']}
        for x in z['response']['items']:
            pid = str(x['id']); assert pid not in rows
            assert x.get('is_archived') or x.get('is_autoarchived')
            x = {**x, '_source': str(path.relative_to(ROOT)), '_observed_at_utc': z['at_utc']}
            rows[pid] = x
    assert set(rows) == pids
    return rows, summary


def stocked(x):
    stocks = x.get('stocks') or {}
    amount = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stocks.get('stocks') or []), D(0))
    return stocks.get('has_stock') is True and amount > 0, amount


def day(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(TZ).date().isoformat() if value else ''


def write(name, rows):
    assert rows
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


obs = read(O / 'publication_observation_DB_snapshot.json')
obsmap = {(r['showcase'], r['offer']): r for r in obs['rows']}
data, windows = {}, {}
for account, path in [('oz_acc1', O / 'oz_acc1_archive'),
                      ('oz_acc2', ROOT / 'ozon_child_gap_audit_20261108' / 'acc2_archive')]:
    data[account], windows[account] = cards(account, path)
stats = {}
rows2, rows1 = [], []
daily = collections.defaultdict(lambda: {'all_auto_children': 0, 'positive_stock': 0})
for account, items in data.items():
    auto = [x for x in items.values() if x.get('is_autoarchived') is True]
    child = lambda x: bool(re.fullmatch(r'[0-9]{5,}', x['offer_id']) if account == 'oz_acc1' else re.match(r'^[0-9]{5}', x['offer_id']))
    achild = [x for x in auto if child(x)]
    stats[account] = {'archived_total': len(items), 'autoarchived_total': len(auto),
                      'archived_without_auto_flag': len(items) - len(auto),
                      'autoarchived_children': len(achild),
                      'autoarchived_children_positive_stock': sum(stocked(x)[0] for x in achild),
                      'autoarchived_ordinary_four_digit_articles': sum(bool(re.fullmatch(r'[0-9]{4}', x['offer_id'])) for x in auto),
                      'source_finished_at_utc': windows[account]['finished_at_utc'],
                      'complete_archived_product_info_coverage': True}
    for x in achild if account == 'oz_acc2' else auto:
        stock, amount = stocked(x)
        observation = obsmap.get((account, x['offer_id']), {})
        created = x.get('created_at', '')
        row = {'account': account, 'product_id': str(x['id']), 'sku': str(x.get('sku') or ''),
               'offer_id': x['offer_id'], 'name': x['name'], 'is_autoarchived': True,
               'is_child': child(x), 'created_at_utc_NOT_PUBLICATION_DATE': created,
               'created_date_msk_NOT_PUBLICATION_DATE': day(created),
               'sku_created_at_utc_NOT_PUBLICATION_DATE': ';'.join(s['created_at'] for s in x.get('sources') or [] if s.get('created_at')),
               'first_publication_at': '', 'first_publication_verified': False,
               'archive_entry_at': '', 'archive_entry_date_verified': False,
               'current_free_stock': str(amount), 'current_positive_stock': stock,
               'old_status_name_NOT_ARCHIVE_OVERRIDE': (x.get('statuses') or {}).get('status_name', ''),
               'status_updated_at_NOT_PUBLICATION_OR_ARCHIVE_DATE': (x.get('statuses') or {}).get('status_updated_at', ''),
               'observation_seeded': observation.get('seeded', ''),
               'observation_first_seen_NOT_PUBLICATION': observation.get('first_observed', ''),
               'source': x['_source'], 'observed_at_utc': x['_observed_at_utc'],
               'automatic_action_allowed': False}
        if account == 'oz_acc2':
            rows2.append(row)
            daily[day(created)]['all_auto_children'] += 1
            daily[day(created)]['positive_stock'] += stock
        else:
            rows1.append(row)
write('acc2_autoarchived_children_dates.csv', rows2)
write('acc1_autoarchived_all.csv', rows1)
cohorts = [{'created_date_msk_NOT_PUBLICATION_DATE': k, **v} for k,v in sorted(daily.items())]
write('acc2_creation_cohorts.csv', cohorts)
assert len(rows2) == 4383 and sum(r['current_positive_stock'] for r in rows2) == 3519
assert stats['oz_acc1']['autoarchived_total'] == 32 and stats['oz_acc1']['autoarchived_children'] == 0
assert stats['oz_acc2']['autoarchived_total'] == 4492
assert all(not r['first_publication_verified'] and not r['archive_entry_date_verified'] for r in rows2 + rows1)
relevant_observations = [obsmap[('oz_acc2', r['offer_id'])] for r in rows2 if ('oz_acc2', r['offer_id']) in obsmap]
assert all(r['active_at'] is None and r['seeded'] for r in relevant_observations)
summary = {'accounts': stats, 'acc2_auto_child_created_from_utc': min(r['created_at_utc_NOT_PUBLICATION_DATE'] for r in rows2),
           'acc2_auto_child_created_through_utc': max(r['created_at_utc_NOT_PUBLICATION_DATE'] for r in rows2),
           'acc2_creation_cohorts_msk': cohorts, 'acc2_observation_rows_for_auto_children': len(relevant_observations),
           'relevant_observations_all_seeded_no_active_at': True,
           'first_publication_dates_verified': False, 'archive_entry_dates_verified': False,
           'date_basis': 'Seller created_at = card creation, sources.created_at = SKU creation; neither asserted first publication',
           'individual_autoarchive_trigger_verified': False,
           'current_help_HTTP': read(O / 'official_help_fetch.json')['http'],
           'source_windows': windows, 'new_ads_reports': 0, 'Ozon_configuration_writes': 0, 'production_DB_writes': 0}
save('dates_summary.json', summary)
rules = {'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
         'official_2024_criteria': {'url': 'https://seller.ozon.ru/media/news/o-nepopulyarnyh-tovarah-obnovili-pravila-ih-avtoarhivacii/',
                                    'publication_date': '2024-03-29', 'window_days': 60,
                                    'joint_criteria': ['at least half the observation window stocks only at seller warehouse',
                                                       'no delivered orders', 'no billed advertising expense and no enabled stencils at check time']},
         'official_2026_clarification': {'url': 'https://seller.ozon.ru/media/news/novoe-v-dogovore-s-18-iyunya-2026-goda/',
                                         'publication_date': '2026-06-04', 'effective_date': '2026-06-18',
                                         'autoarchive_unwanted_products_section': '1.5', 'duplicate_card_hiding_section': '1.6',
                                         'rule_content_unchanged_according_to_news': True},
         'official_flag_meaning': 'https://dev.ozon.ru/start/293-Seller-API-arkhivatsiia-i-razarkhivatsiia-tovarov/',
         'current_full_help_and_contract_direct_read': 'help HTTP403 challenge; web reader returned no text; full contract not readable',
         'individual_criteria_history_reconstructed': False, 'do_not_assume_current_60day_history_proves_past_archive_cause': True}
save('official_rule_evidence.json', rules)
slug = '/api/wb-bids/reports/ozon-autoarchive-dates-2026-11-08'
body = '<h1>Ozon · даты дочерних и автоархив обоих аккаунтов</h1><h2>Даты акк2</h2><p>Все4 383автоархивные дочерние созданы3–7апреля2026. В этой группе3 519с положительным свободным остатком. Это одна календарная группа создания, а не доказанная дата единого импорта или архивирования.</p>'
body += '<p><b>Точная дата первой публикации не установлена.</b> Seller created_at — дата создания карточки, sources.created_at — дата создания SKU. Их не подменяем первой публикацией. В доступной базе наблюдений active_at отсутствует, записи seeded: карточки уже существовали на момент начала наблюдения. status_updated_at также не является датой первой публикации или попадания в архив.</p>'
body += '<table><tr><th>Дата создания, МСК</th><th>Автоархивных дочерних</th><th>Из них с остатком</th></tr>'
for r in cohorts:
    body += '<tr>'+''.join('<td>'+html.escape(str(r[k]))+'</td>' for k in ['created_date_msk_NOT_PUBLICATION_DATE', 'all_auto_children', 'positive_stock'])+'</tr>'
body += '<tr><th>Всего</th><th>4383</th><th>3519</th></tr></table><h2>Есть ли автоархив на акк1</h2><table><tr><th>Показатель</th><th>Акк1</th><th>Акк2</th></tr>'
for key,label in [('archived_total','Всего в архиве'), ('autoarchived_total','Из них автоархив'), ('autoarchived_children','Дочерние в автоархиве'), ('autoarchived_children_positive_stock','Дочерние в автоархиве с остатком')]:
    body += f'<tr><td>{label}</td><td>{stats["oz_acc1"][key]}</td><td>{stats["oz_acc2"][key]}</td></tr>'
body += '</table><p>Все32автоархивные на акк1 имеют обычный четырёхзначный артикул. Среди них нет дочерних и бандлов. Примеры:1060,2541,2459,2543,1655,2102. Архивные акк1 покрыты25 956/25 956; данныеакк2 повторно не выгружались, использован последний успешный архив4 730/4 730.</p>'
body += '<h2>Что означает автоархив</h2><p>Ozon автоматически скрывает невостребованные предложения с витрины; карточка получает is_autoarchived=true. Это отдельный механизм от скрытия дублирующих карточек. Остаток в Seller API может сохраняться, но сам по себе не отменяет архивный статус.</p>'
body += '<p>В <a href="'+rules['official_2024_criteria']['url']+'">опубликованном правиле Ozon от29.03.2024</a> перечислены совместные условия: за60дней не было доставленных заказов и расходов на продвижение; хотя бы половину этого времени остатки были только на складах продавца; в момент проверки трафареты не подключены. Условия относятся к конкретному товару. Так описывается невостребованность карточки, а не просто нулевой остаток.</p>'
body += '<p><a href="'+rules['official_2026_clarification']['url']+'">Уточнение Ozon от04.06.2026</a> разделяет автоархивацию невостребованных товаров(п1.5) и скрытие дублей(п1.6) и говорит, что содержание правил не меняется. Полная текущая справка/договор не прочитаны: справка возвращает403challenge. Поэтому здесь зафиксированы доступные официальные публикации с датами, а не выдумана точная действующая причина каждой карточки.</p>'
body += '<p><b>Почему именно каждая из4 383попала в автоархив, индивидуально не подтверждено.</b> История заказов, остатков, расходов и момента проверки за соответствующий период до архивации не восстановлена. Дата создания в апреле не доказывает дату архивирования. Текущие нулевые продажи архивной карточки не доказывают отсутствие спроса до её скрытия.</p>'
body += f'<p><a href="{slug}-acc2">Все4 383дочерние акк2 с датами CSV</a> · <a href="{slug}-cohorts">Группы по датам CSV</a> · <a href="{slug}-acc1">32автоархивные акк1 CSV</a> · <a href="/api/wb-bids/reports/ozon-child-gap-2026-11-08">Сопоставление дочерних акк1/акк2</a>.</p>'
body += '<p>Ничего не разархивировано. Следующий шаг для даты первой публикации — получить историю первого выхода в продажу/журнал кабинета; для индивидуального триггера — историю перед датой архивации или пояснение Ozon. Создание карточек и дату публикации не смешивать.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: даты и автоархив</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:25px auto;padding:0 16px;color:#172239}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:10px;text-align:left}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O / 'RESULT.md').write_text('# Ozon: даты автоархивных дочерних\n\n'+json.dumps({k:v for k,v in summary.items() if k != 'source_windows'},ensure_ascii=False,indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps({k:v for k,v in summary.items() if k != 'source_windows'},ensure_ascii=False))
