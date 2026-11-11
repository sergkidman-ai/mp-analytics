# поток: mkt / ozon
"""Explain native glue excluding children; reuse saved catalog and eight exact readbacks."""
import collections
import csv
import datetime as dt
import html
import json
from pathlib import Path

O = Path(__file__).parent / 'model_comparison'


def save(name, obj):
    (O / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def output(name, rows):
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def table(head, rows):
    return '<table><tr>' + ''.join('<th>' + html.escape(str(x)) + '</th>' for x in head) + '</tr>' + ''.join(
        '<tr>' + ''.join('<td>' + html.escape(str(x)) + '</td>' for x in r) + '</tr>' for r in rows) + '</table>'


archive = O / 'before_child_exclusion'
archive.mkdir(exist_ok=True)
for name in ['index.html', 'RESULT.md', 'model_comparison_summary.json']:
    if not (archive / name).exists():
        (archive / name).write_bytes((O / name).read_bytes())
old = json.loads((archive / 'model_comparison_summary.json').read_text())
members = list(csv.DictReader((O / 'native_model_members.csv').open(encoding='utf-8-sig')))
metrics, groups_out, child_stats, anomaly_rows, readback_times = {}, [], {}, [], {}
for account in ['oz_acc1', 'oz_acc2']:
    rr = [x for x in members if x['account'] == account]
    child = [x for x in rr if x['kind'] == 'child']
    all_groups = collections.defaultdict(list)
    for x in rr:
        if x['model_id']:
            all_groups[x['model_id']].append(x)
    bad_groups = {m: v for m, v in all_groups.items() if len(v) >= 2 and any(x['kind'] == 'child' for x in v)}
    z = json.loads((O / f'children_shared_model_readback_{account}.json').read_text())
    assert z['account'] == account and z['http'] == 200
    fresh = {str(x['id']): x for x in z['response']['items']}
    assert set(fresh) == {x['product_id'] for v in bad_groups.values() for x in v}
    readback_times[account] = z['at_utc']
    for mid, items in bad_groups.items():
        for x in items:
            f = fresh[x['product_id']]
            assert str(f['model_info']['model_id']) == mid
            assert not f['is_archived'] and not f['is_autoarchived']
            anomaly_rows.append({**{k: x[k] for k in ['account', 'product_id', 'sku', 'offer_id', 'model_id', 'kind', 'in_stock']},
                                 'name': f['name'], 'native_total_count': f['model_info'].get('count'),
                                 'confirmed_at_utc': z['at_utc'], 'action': 'REVIEW_ONLY_NO_CARD_CHANGES'})
    child_stats[account] = {'nonarchived_children': len(child), 'in_stock_children': sum(x['in_stock'] == 'True' for x in child),
                            'single_own_card_model_children': sum(len(all_groups[x['model_id']]) == 1 for x in child if x['model_id']),
                            'children_sharing_native_model_with_own_card': sum(x['kind'] == 'child' for v in bad_groups.values() for x in v),
                            'groups_with_children_2plus': len(bad_groups)}
    for scope in ['all_nonarchived', 'stock_nonarchived']:
        core = [x for x in rr if x['kind'] != 'child' and (scope == 'all_nonarchived' or x['in_stock'] == 'True')]
        groups = collections.defaultdict(list)
        for x in core:
            if x['model_id']:
                groups[x['model_id']].append(x)
        metrics[account + '_' + scope] = {
            'cards_excluding_children': len(core), 'model_ids_including_singletons': len(groups),
            'glue_groups_2plus_excluding_children': sum(len(v) >= 2 for v in groups.values()),
            'single_card_models_excluding_children': sum(len(v) == 1 for v in groups.values()),
            'cards_in_glue_excluding_children': sum(len(v) for v in groups.values() if len(v) >= 2),
            'missing_model_id': sum(not x['model_id'] for x in core)}
        for mid, items in groups.items():
            kinds = collections.Counter(x['kind'] for x in items)
            groups_out.append({'account': account, 'scope': scope, 'model_id': mid, 'our_nonchild_cards': len(items),
                               'family_codes': ';'.join(sorted({x['article_family'] for x in items})),
                               'composition': ';'.join(f'{k}:{n}' for k, n in sorted(kinds.items())),
                               'offer_ids': ';'.join(sorted(x['offer_id'] for x in items))})
summary = {'corrected_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
           'definition': 'native model_id shared by >=2 own nonarchived nonchild cards; singleton is not glue',
           'metrics_excluding_children': metrics, 'children_separate': child_stats,
           'source_windows_utc': old['source_windows_utc'], 'targeted_child_readbacks_utc': readback_times,
           'child_readback_requests': 2, 'child_readback_cards': len(anomaly_rows),
           'equal_counts_do_not_prove_equal_composition': True,
           'article_family_is_not_native_glue': True, 'cross_account_same_native_id_not_required': True,
           'ad_or_card_mutations': 0}
save('model_comparison_summary.json', summary)
output('core_native_model_groups.csv', groups_out)
output('child_shared_model_anomalies.csv', anomaly_rows)

body = '<h1>Склейки акк1 и акк2: дочерние исключены</h1>'
body += '<p><b>Карты не одинаковые.</b> Предыдущая таблица показывала число групп и включала дочерние. Близкое количество групп ничего не говорит о равенстве их состава. Исправленный расчёт ниже включает только основные товары и бандлы.</p>'
body += '<p>Склейка здесь — один фактический Ozon model_id с двумя и более нашими карточками. У одиночной карточки тоже есть model_id, но сама по себе она не склейка. Учитываются только наши известные карточки, не весь маркетплейс.</p>'
body += '<h2>Неархивные товары в наличии, без дочерних</h2>'
a, b = metrics['oz_acc1_stock_nonarchived'], metrics['oz_acc2_stock_nonarchived']
body += table(['Показатель', 'Акк1', 'Акк2'], [
    ['Основные и бандлы', a['cards_excluding_children'], b['cards_excluding_children']],
    ['Склейки с 2+ нашими основными/бандлами', a['glue_groups_2plus_excluding_children'], b['glue_groups_2plus_excluding_children']],
    ['Карточек внутри этих склеек', a['cards_in_glue_excluding_children'], b['cards_in_glue_excluding_children']],
    ['Одиночные основные/бандлы', a['single_card_models_excluding_children'], b['single_card_models_excluding_children']]])
body += '<p>На акк2 на 22 склейки меньше (−1,20%), при этом карточек внутри них на 750 больше. Следовательно, даже при близком количестве групп наполненность различается.</p>'
body += '<p>Типичная группа акк1: основная + X2 + X6 + X10. Акк2: основная + X3 + X6 + X9 + X12. Поэтому одинаковым состав групп назвать нельзя. Числовые model_id также ведутся отдельно для каждого аккаунта.</p>'
aa, bb = metrics['oz_acc1_all_nonarchived'], metrics['oz_acc2_all_nonarchived']
body += f'<p>Если включить неархивные товары с нулевым остатком, также без дочерних: {aa["glue_groups_2plus_excluding_children"]} / {bb["glue_groups_2plus_excluding_children"]} склеек. Это другой срез, его нельзя смешивать с наличием.</p>'
body += '<h2>Дочерние — отдельные карточки. Подтверждённые исключения</h2>'
body += table(['Дочерние', 'Акк1', 'Акк2'], [
    ['Всего неархивных', child_stats['oz_acc1']['nonarchived_children'], child_stats['oz_acc2']['nonarchived_children']],
    ['Из них в наличии', child_stats['oz_acc1']['in_stock_children'], child_stats['oz_acc2']['in_stock_children']],
    ['Отдельный model_id среди наших карточек', child_stats['oz_acc1']['single_own_card_model_children'], child_stats['oz_acc2']['single_own_card_model_children']],
    ['Дочерних в фактических общих model_id', child_stats['oz_acc1']['children_sharing_native_model_with_own_card'], child_stats['oz_acc2']['children_sharing_native_model_with_own_card']]])
body += '<p>Восемь карточек из четырёх групп перепроверены отдельным запросом API 8 ноября. Во всех четырёх случаях Ozon возвращает общий model_id и count=2. Это фактические исключения из ожидаемого устройства дочерних. Автоматически склейки не менялись.</p>'
anomaly_groups = collections.defaultdict(list)
for r in anomaly_rows:
    anomaly_groups[r['account'], r['model_id']].append(r)
body += table(['Аккаунт', 'Дочерние артикулы', 'model_id', 'Что объединено'], [
    [acc, ' + '.join(x['offer_id'] for x in rr), mid, ' / '.join(x['name'] for x in rr)] for (acc, mid), rr in anomaly_groups.items()])
body += '<p>Первые четыре цифры артикула обозначают связь с товарной семьёй для аналитики. Они сами по себе не означают, что карточки склеены в Ozon. Дочерним не требуется наличие родителя для допуска в рекламную волну.</p>'
slug = '/api/wb-bids/reports/ozon-model-map-comparison-2026-11-08'
body += f'<p><a href="{slug}-groups">Состав групп без дочерних, CSV</a> · <a href="{slug}-children">Восемь исключений, SKU и подтверждение, CSV</a> · <a href="{slug}-members">Полный справочник наших карточек с типами, CSV</a>.</p>'
body += '<p>Источник: сохранённые каталоги 08.11 около 21:06 МСК, дополненные 107 восстановленными карточками акк2 по readback. Для исключений — два свежих точечных запроса по восьми ID. Общая карта не выгружалась повторно; срезы не атомарные.</p>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Склейки Ozon без дочерних</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:25px auto;padding:0 16px}td,th{padding:10px;border:1px solid #ddd;text-align:left;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>' + body + '</html>')
(O / 'RESULT.md').write_text('# Исправленное сравнение склеек\n\nДочерние исключены из сравнительных показателей; четыре фактические пары подтверждены API. Равенство карт не доказано; состав бандлов различается.\n\n' + json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'stock_core_glue': [a['glue_groups_2plus_excluding_children'], b['glue_groups_2plus_excluding_children']],
                  'anomaly_cards': len(anomaly_rows), 'anomaly_groups': len(anomaly_groups)}, ensure_ascii=False))
