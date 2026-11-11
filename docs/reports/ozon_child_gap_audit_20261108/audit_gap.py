# поток: mkt / ozon
"""Read saved catalogs, match child cards, distinguish archive from no-stock/missing."""
import collections
import csv
import html
import json
import re
import sys
import unicodedata
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, '/opt/mp-analytics')
from collectors.launch_cards import classify

O = Path(__file__).parent
BASE = O.parent / 'ozon_accounts_inventory_compare_20261108'
read = lambda p: json.loads(p.read_text())
save = lambda n, x: (O / n).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')
numeric_resource = r'(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?)'
resource = re.compile(r'\s+(?:на\s+)?' + numeric_resource + r'\s*(?:страниц[аы]?|стр\.?|метр(?:ов|а)?|мл\.?)\s*\.?$', re.I)


def normal_title(title):
    text = unicodedata.normalize('NFKC', title).casefold().replace('ё', 'е')
    text = re.sub(r'\bds\b', '', text)
    return re.sub(r'\s+', ' ', resource.sub('', text)).strip()


def printer_key(title):
    text = normal_title(title)
    if ' для ' not in text:
        return ''
    text = text.rsplit(' для ', 1)[1]
    text = re.sub(r'^принтер(?:а|ов)?\s+', '', text)
    return ''.join(ch for ch in text if ch.isalnum())


def load_cards(account, root, visibility):
    a = read(root / 'acquisition_summary.json')
    assert a['account'] == account
    ids = set(map(str, read(root / f'product_ids_{visibility}.json')))
    cards = {}
    for path in sorted((root / 'raw').glob(f'info_{visibility}_*.json')):
        z = read(path)
        assert z['account'] == account
        if z['http'] != 200:
            continue
        assert set(map(str, z['body']['product_id'])) == {str(x['id']) for x in z['response']['items']}
        for item in z['response']['items']:
            pid = str(item['id']); assert pid not in cards
            offer = item['offer_id']
            role = classify(offer, item['name'], account)
            group = 'bundle' if role and role[1] == 'bundle' else 'child' if (re.fullmatch(r'[0-9]{5,}', offer) if account == 'oz_acc1' else re.match(r'^[0-9]{5}', offer)) else 'other'
            stocks = item.get('stocks') or {}
            available = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stocks.get('stocks') or []), D(0))
            archived = bool(item.get('is_archived') or item.get('is_autoarchived'))
            stock = stocks.get('has_stock') is True and available > 0
            status = 'AUTOARCHIVED' if item.get('is_autoarchived') else 'ARCHIVED_OTHER' if archived else 'LIVE_IN_STOCK' if stock else 'LIVE_NO_FREE_STOCK'
            cards[pid] = {'account': account, 'product_id': pid,
                          'sku': str(item.get('sku') or next((s['sku'] for s in item.get('sources') or [] if s.get('sku')), '')),
                          'offer_id': offer, 'name': item['name'], 'group': group,
                          'family': offer[:4], 'normalized_title': normal_title(item['name']),
                          'printer_key': printer_key(item['name']), 'archived': archived,
                          'autoarchived': bool(item.get('is_autoarchived')), 'free_stock': str(available),
                          'stock': stock, 'status': status, 'created_at': item.get('created_at', ''),
                          'status_updated_at_NOT_ARCHIVE_DATE': (item.get('statuses') or {}).get('status_updated_at', ''),
                          'source': str(path.relative_to(O.parent)), 'observed_at_utc': z['at_utc']}
    assert set(cards) == ids
    return cards, a


acc1, a1 = load_cards('oz_acc1', BASE / 'oz_acc1', 'all')
acc2_live, a2 = load_cards('oz_acc2', BASE / 'oz_acc2', 'all')
acc2_arch, a3 = load_cards('oz_acc2', O / 'acc2_archive', 'archived')
assert not set(acc2_live) & set(acc2_arch)
acc2 = {**acc2_live, **acc2_arch}
reference = [r for r in acc1.values() if r['group'] == 'child' and not r['archived'] and r['stock']]
assert len(reference) == 6206
children2 = [r for r in acc2.values() if r['group'] == 'child']
title_index, printer_index = collections.defaultdict(list), collections.defaultdict(list)
by_family = collections.defaultdict(list)
for r in children2:
    title_index[(r['family'], r['normalized_title'])].append(r)
    if r['printer_key']:
        printer_index[(r['family'], r['printer_key'])].append(r)
    by_family[r['family']].append(r)

mapping, edges = [], []
categories = collections.Counter()
method_count = collections.Counter()
priority = {'LIVE_IN_STOCK': 0, 'LIVE_NO_FREE_STOCK': 1, 'AUTOARCHIVED': 2, 'ARCHIVED_OTHER': 3}
ref_keys = collections.Counter((r['family'], r['printer_key']) for r in reference if r['printer_key'])
for r in reference:
    hits = title_index.get((r['family'], r['normalized_title']), [])
    method = 'same_family_exact_title_after_resource_DS_trim'
    if not hits:
        hits = printer_index.get((r['family'], r['printer_key']), []) if r['printer_key'] else []
        method = 'same_family_exact_printer_name_alnum'
    hits = sorted(hits, key=lambda h: (priority[h['status']], h['product_id']))
    states = sorted({h['status'] for h in hits}, key=priority.get)
    if hits:
        category = states[0]
    else:
        category = 'NO_CHILD_FAMILY_IN_ACC2' if not by_family[r['family']] else 'NO_EXACT_PRINTER_MATCH_REVIEW'
        method = 'no_confirmed_match'
    categories[category] += 1
    method_count[method] += 1
    if hits:
        for h in hits:
            edges.append({'acc1_offer': r['offer_id'], 'acc1_sku': r['sku'], 'acc1_name': r['name'],
                          'acc2_offer': h['offer_id'], 'acc2_sku': h['sku'], 'acc2_product_id': h['product_id'], 'acc2_name': h['name'],
                          'acc2_status': h['status'], 'acc2_free_stock': h['free_stock'],
                          'family': r['family'], 'method': method, 'acc2_source': h['source'],
                          'acc2_observed_at_utc': h['observed_at_utc']})
    mapping.append({'acc1_offer': r['offer_id'], 'acc1_sku': r['sku'], 'acc1_name': r['name'],
                    'family': r['family'], 'acc1_stock': r['free_stock'], 'acc1_created_at': r['created_at'],
                    'acc1_ordinal': r['offer_id'][4:], 'category': category,
                    'match_method': method, 'match_count': len(hits),
                    'reference_printer_key_duplicate_count': ref_keys[(r['family'], r['printer_key'])] if r['printer_key'] else '',
                    'candidate_states': ';'.join(states),
                    'acc2_offers': ';'.join(h['offer_id'] for h in hits),
                    'acc2_SKUs': ';'.join(h['sku'] for h in hits),
                    'acc2_names': ' || '.join(h['name'] for h in hits),
                    'acc2_stock_values': ';'.join(h['free_stock'] for h in hits),
                    'acc1_source': r['source'], 'acc2_sources': ';'.join(h['source'] for h in hits),
                    'automatic_action_allowed': False})


def write(name, rows, fields=None):
    if fields is None:
        fields = list(rows[0])
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)


write('acc1_stock_children_to_acc2.csv', mapping)
write('matched_counterpart_edges.csv', edges)
for category in categories:
    write(category + '.csv', [r for r in mapping if r['category'] == category], list(mapping[0]))
write('acc2_all_children_with_archive.csv', children2)
fam_rows = []
for family in sorted({r['family'] for r in reference}):
    ref = [r for r in mapping if r['family'] == family]
    own = by_family[family]
    fam_rows.append({'family': family, 'acc1_stock_children': len(ref),
                     'acc2_stock_children': sum(r['status'] == 'LIVE_IN_STOCK' for r in own),
                     'acc2_live_no_stock_children': sum(r['status'] == 'LIVE_NO_FREE_STOCK' for r in own),
                     'acc2_autoarchived_children': sum(r['status'] == 'AUTOARCHIVED' for r in own),
                     'acc2_other_archived_children': sum(r['status'] == 'ARCHIVED_OTHER' for r in own),
                     'acc2_main_instock': sum(r['family'] == family and r['group'] == 'other' and r['status'] == 'LIVE_IN_STOCK' for r in acc2.values()),
                     'acc1_reference_matched_autoarchive': sum(r['category'] == 'AUTOARCHIVED' for r in ref),
                     'acc1_reference_no_confirmed_match': sum(r['match_count'] == 0 for r in ref)})
fam_rows.sort(key=lambda r: (-(r['acc1_stock_children'] - r['acc2_stock_children']), r['family']))
write('family_gaps.csv', fam_rows)
ordinal = collections.defaultdict(collections.Counter)
for r in mapping:
    n = int(r['acc1_ordinal'])
    bucket = '1' if n == 1 else '2-5' if n <= 5 else '6-10' if n <= 10 else '11-20' if n <= 20 else '21-50' if n <= 50 else '51+'
    ordinal[bucket][r['category']] += 1
archive_counter = collections.Counter((r['status'], r['stock']) for r in children2 if r['archived'])
summary = {'reference_acc1_stock_children': len(reference), 'acc1_nonarchived_children_total': sum(r['group'] == 'child' for r in acc1.values()),
           'acc2_nonarchived_children_total': sum(r['group'] == 'child' for r in acc2_live.values()),
           'acc2_nonarchived_stock_children': sum(r['group'] == 'child' and r['stock'] for r in acc2_live.values()),
           'acc2_nonarchived_children_without_stock': sum(r['group'] == 'child' and not r['stock'] for r in acc2_live.values()),
           'acc2_archived_children_total': sum(r['group'] == 'child' for r in acc2_arch.values()),
           'acc2_archived_children_auto': sum(r['group'] == 'child' and r['autoarchived'] for r in acc2_arch.values()),
           'acc2_archived_children_other': sum(r['group'] == 'child' and not r['autoarchived'] for r in acc2_arch.values()),
           'acc2_autoarchived_children_positive_stock': archive_counter[('AUTOARCHIVED', True)],
           'acc2_other_archived_children_positive_stock': archive_counter[('ARCHIVED_OTHER', True)],
           'reference_counterpart_statuses': dict(categories), 'reference_matching_methods': dict(method_count),
           'reference_with_multiple_acc2_candidates': sum(r['match_count'] > 1 for r in mapping),
           'reference_with_duplicate_family_printer_key': sum(r['reference_printer_key_duplicate_count'] != '' and r['reference_printer_key_duplicate_count'] > 1 for r in mapping),
           'matched_distinct_acc2_product_ids': len({r['acc2_product_id'] for r in edges}),
           'family_count_acc1_reference': len(fam_rows),
           'reference_families_with_no_acc2_children_any_state': sum(not by_family[r['family']] for r in fam_rows),
           'reference_families_with_zero_live_stock_children_but_autoarchive': sum(r['acc2_stock_children'] == 0 and r['acc2_autoarchived_children'] > 0 for r in fam_rows),
           'reference_families_with_zero_live_stock_children_but_main_instock': sum(r['acc2_stock_children'] == 0 and r['acc2_main_instock'] > 0 for r in fam_rows),
           'acc1_ordinal_buckets_by_counterpart_status': {k: dict(v) for k,v in ordinal.items()},
           'source_windows': {'acc1_live': a1, 'acc2_live': a2, 'acc2_archive': a3},
           'coverage': {'acc1_live': len(acc1), 'acc2_live': len(acc2_live), 'acc2_archive': len(acc2_arch)},
           'card_matching_is_not_seller_SKU_or_ordinal_equality': True,
           'unmatched_is_not_proof_never_created_or_deleted': True,
           'archive_time_and_exact_trigger_not_proven': True,
           'new_ads_reads': 0, 'Ozon_configuration_writes': 0, 'production_DB_writes': 0}
assert sum(categories.values()) == 6206
assert summary['acc2_archived_children_total'] == 4416
assert summary['acc2_archived_children_auto'] == 4383
assert summary['acc2_autoarchived_children_positive_stock'] == 3519
save('gap_summary.json', summary)
labels = {'LIVE_IN_STOCK': 'Соответствие на акк2 неархивное, в наличии',
          'LIVE_NO_FREE_STOCK': 'Соответствие есть, но нет свободного остатка',
          'AUTOARCHIVED': 'Соответствие находится в автоархиве акк2',
          'ARCHIVED_OTHER': 'Соответствие в архиве без признака автоархивации',
          'NO_CHILD_FAMILY_IN_ACC2': 'На акк2 не найдены дочерние этого семейства ни в каталоге, ни в архиве',
          'NO_EXACT_PRINTER_MATCH_REVIEW': 'Семейство есть, точное соответствие принтера не найдено; требуется проверка'}
slug = '/api/wb-bids/reports/ozon-child-gap-2026-11-08'
body = '<h1>Почему на акк2 меньше неархивных дочерних в наличии</h1><p><b>Главный подтверждённый фактор — автоархив акк2.</b> Текущая неархивная выборка1 110SKU верна по фильтру, но сама по себе не объясняла, куда делись остальные дочерние.</p>'
body += '<p>На акк2 дополнительно найдено4 416архивных дочерних:4 383с признаком автоархивации и33без него. У3 519автоархивных API показывает положительный свободный остаток. Ещё7дочерних с положительным остатком находятся в другом архиве. Архивные карточки не входят в запрошенные неархивные, даже при наличии остатка.</p>'
body += '<h2>Что найдено для6 206дочерних в наличии на акк1</h2><table><tr><th>Результат сопоставления</th><th>Карточек акк1</th><th>Список</th></tr>'
for category in list(priority) + [c for c in categories if c not in priority]:
    if categories[category]:
        body += f'<tr><td>{html.escape(labels[category])}</td><td>{categories[category]}</td><td><a href="{slug}-{category}">CSV</a></td></tr>'
body += '</table><p>Единица таблицы — исходная карточка акк1. Это не сумма уникальных SKU акк2. При нескольких соответствиях приоритет: неархивное с остатком, неархивное без остатка, автоархив, прочий архив. Все кандидаты и основания сохранены, неоднозначность не скрыта.</p>'
body += f'<p>Несколько кандидатов на акк2 у {summary["reference_with_multiple_acc2_candidates"]} исходных карточек; повторяющийся ключ «семейство + принтер» на акк1 у {summary["reference_with_duplicate_family_printer_key"]}. Для отсутствующих точных соответствий нельзя утверждать, что карточка никогда не создавалась: возможны другое название или удаление, исторических доказательств нет.</p>'
body += '<h2>По какому принципу сравнивали</h2><p>Порядковые номера дочерних между аккаунтами не являются общим идентификатором. Например, принтерHP DesignJet111Tray: акк1артикул03202, акк2артикул03206M5TEUTB. Сначала совпадение внешнего кода первых4цифр и названия после удаления конечного ресурса/DS; затем точное совпадение нормализованной модели принтера внутри того же внешнего кода. Похожесть текста и один model_id не использовались как доказательство.</p>'
body += '<p>При нормализации не отбрасываются номера принтеров: удаляется только конечное число со словом «страниц/метров/мл». Тип товара допускает разное написание, поэтому дополнительный ключ берёт часть названия после «для». Все сопоставленные исходные названия есть вCSV для проверки.</p>'
body += '<h2>Семейства с наибольшим разрывом</h2><table><tr><th>Код</th><th>Акк1 дочерние в наличии</th><th>Акк2 дочерние неархивные в наличии</th><th>Акк2 дочерние в автоархиве</th><th>Акк2 основные в наличии</th></tr>'
for r in fam_rows[:20]:
    body += '<tr>'+''.join('<td>'+html.escape(str(r[k]))+'</td>' for k in ['family', 'acc1_stock_children', 'acc2_stock_children', 'acc2_autoarchived_children', 'acc2_main_instock'])+'</tr>'
body += '</table>'
body += f'<p>У {summary["reference_families_with_zero_live_stock_children_but_autoarchive"]} семейств из выборки акк1 на акк2 нет неархивных дочерних с остатком, но есть дочерние в автоархиве. У {summary["reference_families_with_zero_live_stock_children_but_main_instock"]} семейств нет неархивных дочерних с остатком, при этом основная карточка в наличии. Наличие основной не означает, что отсутствуют дочерние физически — их архивный статус проверен отдельно.</p>'
body += '<h2>Границы вывода</h2><p>Сами флаги is_autoarchived подтверждают автоматический архив. Точная причина/дата переноса каждой карточки не подтверждены: status_updated_at не считается датой архивации, а старое status_name «Продается» не отменяет is_archived/is_autoarchived. Нельзя по этому срезу доказать, что архив вызван снижением ставок или недавним запуском рекламы.</p>'
body += '<p><a href="https://dev.ozon.ru/start/293-Seller-API-arkhivatsiia-i-razarkhivatsiia-tovarov/">Официальное описание флага автоархивации</a>. Исторические публичные критерии Ozon связаны с отсутствием заказов/расходов продвижения, но выполнение этих условий по каждой карточке и действующие индивидуальные причины здесь не установлены.</p>'
body += '<p>Полный неархивный каталог обоих аккаунтов прочитан08.11около21:06МСК; отдельно актуальный архив акк2 позже в этой проверке. Новых широких рекламных выгрузок не было. Все ответы сохранены отдельно от расчётов, аккаунты не смешаны. Архивных карточек акк2 всего4 730, покрытие100%; пересечениеproduct_idс текущим каталогом0.</p>'
body += f'<p><a href="{slug}-all">Все6 206строк сопоставления CSV</a> · <a href="{slug}-edges">Все найденные кандидаты CSV</a> · <a href="{slug}-families">Семейства CSV</a> · <a href="{slug}-archive">Все дочерние акк2, включая архив CSV</a>.</p>'
body += '<p><b>Ничего не разархивировано, остатки и реклама не менялись.</b> Следующий шаг — проверить небольшой список автоархивных с положительным остатком и востребованными моделями принтеров, затем отдельно решить вопрос восстановления. Самостоятельного массового восстановления нет.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: разрыв дочерних</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:25px auto;padding:0 16px;color:#172239}td,th{padding:9px;border:1px solid #ddd;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O / 'RESULT.md').write_text('# Проверка разрыва дочерних\n\n'+json.dumps({k: v for k,v in summary.items() if k not in ['source_windows','acc1_ordinal_buckets_by_counterpart_status']}, ensure_ascii=False, indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps({k:v for k,v in summary.items() if k not in ['source_windows','acc1_ordinal_buckets_by_counterpart_status']}, ensure_ascii=False))
