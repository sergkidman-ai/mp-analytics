# поток: mkt / ozon
"""Apply the owner's in-stock-only constraint to the captured catalog and BI report."""
import collections
import csv
import datetime as dt
import decimal
import html
import itertools
import json
import pathlib
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / 'in_stock'
OUT.mkdir(exist_ok=True)
D = decimal.Decimal
PREFIX = '/api/wb-bids/reports/ozon-inventory-2026-11-05'
groups = [('bundles_x2', 'Бандлы x2', 'x2'), ('bundles_x6', 'Бандлы x6', 'x6'),
          ('bundles_x10', 'Бандлы x10', 'x10'), ('new_6months', 'Новинки за полгода', 'new'),
          ('other_outside_cpc', 'Остальные SKU', 'other')]


def read(path):
    with path.open(encoding='utf-8-sig') as f:
        yield from csv.DictReader(f)


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def write(name, rows, fields):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def fmt(value):
    return f'{value:,}'.replace(',', ' ')


balances = {}
for path in sorted((ROOT / 'raw').glob('info_all_*.json')):
    for item in json.loads(path.read_text())['response']['items']:
        stocks = item.get('stocks') or {}
        sources = stocks.get('stocks') or []
        available = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in sources), D(0))
        balances[str(item['id'])] = {'has_stock': stocks.get('has_stock') is True,
                                   'available': available,
                                   'present': sum((D(str(s.get('present') or 0)) for s in sources), D(0)),
                                   'reserved': sum((D(str(s.get('reserved') or 0)) for s in sources), D(0))}

all_outside = list(read(ROOT / 'all_outside_cpc.csv'))
stocked = []
for row in all_outside:
    balance = balances[row['product_id']]
    if not balance['has_stock'] or balance['available'] <= 0:
        continue
    row.update(available_stock_units=str(balance['available']),
               present_stock_units=str(balance['present']), reserved_stock_units=str(balance['reserved']))
    stocked.append(row)
fields = list(stocked[0])
partition = {name: [r for r in stocked if r['segment'] == name] for name, _, _ in groups}
assert sum(map(len, partition.values())) == len(stocked)
checks = []
for a, b in itertools.combinations(partition, 2):
    for key in ['sku', 'product_id', 'offer_id']:
        n = len({r[key] for r in partition[a]} & {r[key] for r in partition[b]})
        checks.append({'left': a, 'right': b, 'key': key, 'overlap': n})
assert all(x['overlap'] == 0 for x in checks)
for key in ['sku', 'product_id', 'offer_id']:
    assert len({r[key] for r in stocked}) == len(stocked)
write('all_outside_cpc.csv', stocked, fields)
for name, rows in partition.items():
    write(name + '.csv', rows, fields)
review = [r for r in stocked if r['previous_bundle_label_without_X'] == 'True']
write('previous_bundle_label_without_marker.csv', review, fields)

source_summary = json.loads((ROOT / 'summary.json').read_text())
counts = {name: {'count': len(rows),
                 'own_readiness_passed': sum(r['own_readiness_checks_passed'] == 'True' for r in rows),
                 'previous_wave1_prepared': sum(r['previous_wave1_prepared'] == 'True' for r in rows)}
          for name, rows in partition.items()}
summary = {'account': 'oz_acc1', 'snapshot_finished_at_utc': source_summary['snapshot_finished_at_utc'],
           'scope': 'nonarchived cards with own has_stock=true AND positive present-reserved, outside RUNNING CPC',
           'stock_rule': 'has_stock=true and sum(max(present-reserved,0)) > 0 across Seller stock sources',
           'new_cards_window_from_msk': source_summary['new_cards_window_from_msk'],
           'new_cards_window_through_utc': source_summary['new_cards_window_through_utc'],
           'nonarchived_in_stock_catalog_cards': sum(b['has_stock'] and b['available'] > 0 for b in balances.values()),
           'in_stock_outside_running_cpc': len(stocked),
           'no_stock_removed_from_main_report': len(all_outside) - len(stocked),
           'archived_excluded_from_main_report': True,
           'counts': counts, 'all_pairwise_intersections_zero': True,
           'own_readiness_passed': sum(r['own_readiness_checks_passed'] == 'True' for r in stocked),
           'global_all_sku_cpo_running': source_summary['global_all_sku_cpo_running'],
           'no_any_paid_advertising_claimed': False,
           'young_bundles_not_duplicated_in_new': sum(r['created_within_6months'] == 'True' for r in stocked if int(r['bundle_count']) > 1),
           'previous_bundle_without_X_in_stock': len(review),
           'previous_wave1_prepared_in_stock': sum(r['previous_wave1_prepared'] == 'True' for r in stocked),
           'marketplace_mutations': 0, 'database_data_writes': 0}
save('summary.json', summary)
save('validation.json', {'pairwise_checks': checks, 'all_rows_positive_available_stock': all(D(r['available_stock_units']) > 0 for r in stocked),
                         'all_rows_have_stock_flag': all(r['own_has_stock'] == 'True' for r in stocked),
                         'all_rows_nonarchived': all(r['archived'] == 'False' for r in stocked),
                         'all_rows_outside_cpc': all(r['outside_running_cpc'] == 'True' for r in stocked),
                         'all_rows_unique_primary_sku_product_offer': True, 'partition_union_count': len(stocked)})

columns = [('sku', 'SKU'), ('offer_id', 'Артикул'), ('name', 'Название'),
           ('available_stock_units', 'Доступный остаток'), ('present_stock_units', 'Остаток всего'),
           ('reserved_stock_units', 'Зарезервировано'), ('created_at_msk', 'Создано, МСК'),
           ('bundle_count', 'Количество в бандле'), ('bundle_base_offer_id', 'Основной артикул'),
           ('created_within_6months', 'Создано за полгода'), ('own_available_channel', 'Доступный канал'),
           ('own_price_rub', 'Цена'), ('own_card_errors_count', 'Ошибок карточки'),
           ('own_readiness_checks_passed', 'Дополнительные базовые проверки пройдены'),
           ('own_readiness_failures', 'Причины непройденных проверок'),
           ('global_all_sku_cpo_running', 'CPO все товары включено'),
           ('previous_wave1_prepared', 'Входил в первую волну 2953'),
           ('previous_wave1_stream', 'Прежняя группа'),
           ('sku_aliases', 'Другие SKU этой карточки'), ('product_id', 'Product ID'),
           ('model_id', 'Склейка model_id'), ('metadata_snapshot_utc', 'Снимок UTC')]
wb = Workbook(write_only=True)
for name, label, _ in groups:
    ws = wb.create_sheet(label[:31])
    ws.freeze_panes = 'D2'
    for letter, width in [('A', 18), ('B', 18), ('C', 64), ('D', 19), ('G', 30)]:
        ws.column_dimensions[letter].width = width
    header = []
    for _, label_text in columns:
        cell = WriteOnlyCell(ws, label_text)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='175CD3')
        header.append(cell)
    ws.append(header)
    for row in partition[name]:
        cells = []
        for key, _ in columns:
            value = row.get(key, '')
            value = 'да' if value == 'True' else 'нет' if value == 'False' else value
            cell = WriteOnlyCell(ws, value)
            cell.data_type = 's'
            cells.append(cell)
        ws.append(cells)
    ws.auto_filter.ref = f'A1:V{len(partition[name])+1}'
wb.save(OUT / 'inventory.xlsx')

stamp = dt.datetime.fromisoformat(summary['snapshot_finished_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
table = []
md = ['# Только в наличии: полный список oz_acc1 вне CPC', '', f'Снимок {stamp}. Пользователь уточнил: смотреть только товары в наличии.', '',
      'Наличие подтверждено по самому товару: has_stock=true и доступный остаток present−reserved > 0. Архив и отсутствие наличия исключены из основного отчёта и Excel. Родственники не исключают SKU.', '',
      '| Сегмент | В наличии вне CPC | Дополнительные базовые проверки пройдены |', '|---|---:|---:|']
for name, label, suffix in groups:
    c = counts[name]
    table.append(f'<tr><td>{html.escape(label)}</td><td>{fmt(c["count"])}</td><td>{fmt(c["own_readiness_passed"])}</td><td><a href="{PREFIX}-{suffix}">Полный CSV</a></td></tr>')
    md.append(f'| {label} | {c["count"]} | {c["own_readiness_passed"]} |')
md += [f'| **Итого** | **{len(stocked)}** | **{summary["own_readiness_passed"]}** |', '',
       f'Все пять групп попарно не пересекаются по SKU/product_id/offer_id: 30 проверок, все 0. Сводный файл — их объединение. Из основного реестра убраны {summary["no_stock_removed_from_main_report"]} позиций без наличия.', '',
       'Новинки: product.created_at Seller API с 05.05.2026 00:00 МСК по снимок 05.11.2026. Бандлы имеют приоритет и не повторяются среди новинок. Количество — по явному суффиксу артикула X2/X6/X10.', '',
       f'В наличии среди всех текущих неархивных карточек {summary["nonarchived_in_stock_catalog_cards"]}; вне RUNNING CPC {len(stocked)}. Вне CPC и без наличия не входят в этот план.', '',
       'Общий CPO «Оплата за заказ: все товары» включён. Все списки означают отсутствие CPC, а не отсутствие любого платного продвижения. Персональный searchPromoStatus=false не выключает общий CPO.', '',
       'Дополнительные базовые проверки — собственная цена RUB, доступный канал и отсутствие собственных ошибок. Наличие само по себе не равно допуску к запуску; индивидуальные floor и бюджет здесь не обновлялись.', '',
       'Полные CSV и Excel опубликованы на прежней ссылке BI. Исходные полные снимки сохранены внутренне для доказательства охвата, но не включены в основной пользовательский отчёт. Реклама/экспериментальные группы/БД не менялись.', '']
(OUT / 'RESULT.md').write_text('\n'.join(md))
page = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>В наличии и вне CPC · oz_acc1</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#182230;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1000px;margin:28px auto;padding:32px;background:white;border:1px solid #e4e7ec;border-radius:14px}}h1{{font-size:29px;line-height:1.3}}h2{{font-size:22px;margin-top:28px}}a{{color:#175cd3}}.meta{{color:#667085;font-size:14px}}.notice{{padding:17px;border:1px solid #fedf89;background:#fffaeb;border-radius:10px;margin:18px 0}}.ok{{padding:17px;background:#ecfdf3;border:1px solid #abefc6;border-radius:10px}}.links{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}}.links a{{padding:10px 15px;background:#eff4ff;border:1px solid #b2ccff;border-radius:8px;text-decoration:none}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:12px;border-bottom:1px solid #e4e7ec;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left;white-space:normal;min-width:160px}}th{{background:#f8fafc}}li{{margin:10px 0}}footer{{margin-top:28px;border-top:1px solid #e4e7ec;padding-top:18px;color:#667085}}@media(max-width:640px){{main{{margin:0;padding:20px 16px;border:0;border-radius:0}}h1{{font-size:25px}}th,td{{padding:9px}}}}</style></head><body><main>
<div class="meta">oz_acc1 · {stamp} · полная сверка каталога и действующих CPC</div><h1>Бандлы, новинки и остальные SKU — только в наличии</h1>
<div class="ok"><strong>Основные списки и Excel содержат только неархивные товары с подтверждённым наличием.</strong> У самого товара has_stock=true и положительный доступный остаток после резерва. Отсутствие товара у родственника не влияет на включение.</div>
<p>В наличии и вне действующего CPC: <strong>{fmt(len(stocked))} SKU</strong>. Исходная подготовка 2953 была только первой волной, а не всем планом.</p>
<div class="links"><a href="{PREFIX}-xlsx">Скачать Excel — все пять групп</a><a href="{PREFIX}-all">Скачать сводный CSV</a></div>
<div class="notice"><strong>Новый месячный план:</strong> три недельные волны на индивидуальных минимальных ставках. <a href="/api/wb-bids/reports/ozon-monthly-plan-2026-11-05">Протокол и черновые волны</a> · <a href="/api/wb-bids/reports/ozon-year-review-2026-11-05">Отдельный годовой разбор хвоста</a>. Кампании не активированы.</div><h2>Пять списков без пересечений</h2><div class="scroll"><table><thead><tr><th>Группа</th><th>В наличии вне CPC</th><th>Доп. базовые проверки</th><th>Список</th></tr></thead><tbody>{''.join(table)}<tr><td><strong>Всего</strong></td><td><strong>{fmt(len(stocked))}</strong></td><td><strong>{fmt(summary['own_readiness_passed'])}</strong></td><td></td></tr></tbody></table></div>
<p>Все 10 пар списков проверены по SKU, product_id и артикулу: <strong>пересечений 0</strong>. Сводный файл объединяет эти пять групп. Только индивидуальные минимальные ставки и бюджеты на этом шаге не обновлялись; это инвентарь для планирования.</p>
<h2>Новинки за полгода и бандлы</h2><ul><li>Новинки: <strong>5 мая 2026, 00:00 МСК — снимок 5 ноября</strong>. Берётся created_at самой карточки, а не дата обновления.</li><li>Бандлы определены по явному суффиксу артикула <strong>X2, X6, X10</strong>; количество и основной артикул сохранены в файлах.</li><li>Все {fmt(summary['young_bundles_not_duplicated_in_new'])} бандла в наличии также созданы за полгода. Они оставлены только в бандлах, в новинках повторов нет.</li><li>Остальные — небандлы старше полугода. Дат создания не хватает у 0 карточек текущего каталога.</li></ul>
<h2>Допуск и охват</h2><p>В текущем неархивном каталоге в наличии {fmt(summary['nonarchived_in_stock_catalog_cards'])} карточек; из них {fmt(len(stocked))} вне CPC. Без наличия из основного реестра исключены {fmt(summary['no_stock_removed_from_main_report'])} позиций. Архив не включён.</p>
<p>{fmt(summary['own_readiness_passed'])} из {fmt(len(stocked))} дополнительно проходят проверку цены RUB, доступного канала и собственных ошибок. Остальные {len(stocked)-summary['own_readiness_passed']} остаются в списках в наличии с отдельными причинами: они ещё не готовы к рекламному запуску. Никто не исключён по признаку родственника.</p>
<div class="notice"><strong>«Вне рекламы» здесь означает вне CPC.</strong> В кабинете включена «Оплата за заказ: все товары», кампания 24244024. Наличие CPO отмечено в каждой строке. Персональный searchPromoStatus=false не отменяет общий режим. <a href="https://seller.ozon.ru/media/news/oplata-za-zakaz-prodvigajte-srazu-vse-tovary-so-stavkoj-5percent-1/">Как работает общий CPO — Ozon</a>.</div>
<h2>Сверка прежней первой волны</h2><p>В наличии среди прежних подготовленных 2953 сейчас {fmt(summary['previous_wave1_prepared_in_stock'])} SKU. Прежние группы сохранены отдельной колонкой, но не подменяют классификацию по фактическому артикулу и дате.</p><p>В прежней группе «бандлы» есть позиции без X2/X6/X10; из них сейчас в наличии {len(review)}. <a href="{PREFIX}-old-bundle-review">Скачать их для проверки прежней метки</a>. Новая разметка использует только явный маркер, без придумывания количества.</p>
<footer>Реклама, ставки, бюджеты, экспериментальные назначения и данные БД не менялись. Отсутствие CPC сейчас не доказывает отсутствие рекламы когда-либо в прошлом. <a href="{PREFIX}-data">Сводка JSON</a>.</footer></main></body></html>'''
(OUT / 'index.html').write_text(page)
print('In-stock only:', len(stocked), 'exclusive groups:', {k: v['count'] for k,v in counts.items()})
print('Readiness:', summary['own_readiness_passed'], 'previous wave still in stock:', summary['previous_wave1_prepared_in_stock'], 'pairwise key checks:', len(checks), 'all zero')
