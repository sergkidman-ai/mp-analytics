# поток: mkt / ozon
"""Render already captured inventory as a protected BI report and Excel workbook."""
import collections
import csv
import datetime as dt
import html
import json
import pathlib
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

OUT = pathlib.Path(__file__).parent
PREFIX = '/api/wb-bids/reports/'
KEY = 'ozon-inventory-2026-11-05'
summary = json.loads((OUT / 'summary.json').read_text())
segments = [('bundles_x2', 'Бандлы x2', 'x2'), ('bundles_x6', 'Бандлы x6', 'x6'),
            ('bundles_x10', 'Бандлы x10', 'x10'), ('new_6months', 'Новинки за полгода, без бандлов', 'new'),
            ('other_outside_cpc', 'Остальные SKU вне CPC', 'other')]


def read(name):
    with (OUT / name).open(encoding='utf-8-sig') as f:
        yield from csv.DictReader(f)


def fmt(value):
    return f'{value:,}'.replace(',', ' ')


def link(suffix, label):
    return f'<a href="{PREFIX}{KEY}-{suffix}">{html.escape(label)}</a>'


previous = collections.Counter()
for r in read('all_outside_cpc.csv'):
    if r['previous_wave1_prepared'] == 'True':
        previous[r['segment']] += 1

columns = [('sku', 'SKU'), ('offer_id', 'Артикул'), ('name', 'Название'),
           ('created_at_msk', 'Создано, МСК'), ('bundle_count', 'Количество в бандле'),
           ('bundle_base_offer_id', 'Артикул основной'), ('segment', 'Сегмент'),
           ('created_within_6months', 'Создано за полгода'), ('own_has_stock', 'Собственное наличие'),
           ('own_available_channel', 'Доступный канал'), ('own_price_rub', 'Цена'),
           ('own_card_errors_count', 'Ошибок карточки'),
           ('own_readiness_checks_passed', 'Собственные базовые проверки пройдены'),
           ('own_readiness_failures', 'Причины непройденных проверок'),
           ('global_all_sku_cpo_running', 'CPO все товары включено'),
           ('global_cpo_applies_to_current_own_stock', 'Глобальный CPO при текущем наличии'),
           ('individual_cpo_enabled', 'Индивидуальный CPO включён'),
           ('previous_wave1_prepared', 'Входил в подготовленные 2953'),
           ('previous_wave1_stream', 'Прежняя группа волны 1'),
           ('previous_bundle_label_without_X', 'Прежняя метка бандла без X'),
           ('sku_aliases', 'Другие SKU той же карточки'), ('product_id', 'Product ID'),
           ('model_id', 'Склейка model_id'), ('metadata_snapshot_utc', 'Дата снимка UTC')]
wb = Workbook(write_only=True)
for filename, title in [(name + '.csv', label[:31]) for name, label, _ in segments] + [('archived_outside_cpc.csv', 'Архив вне CPC')]:
    ws = wb.create_sheet(title)
    ws.freeze_panes = 'C2'
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 64
    ws.column_dimensions['D'].width = 30
    header = []
    for _, label in columns:
        c = WriteOnlyCell(ws, label)
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='175CD3')
        header.append(c)
    ws.append(header)
    count = 1
    for r in read(filename):
        cells = []
        for key, _ in columns:
            value = r.get(key, '')
            value = 'да' if value == 'True' else 'нет' if value == 'False' else value
            cell = WriteOnlyCell(ws, value)
            cell.data_type = 's'
            cells.append(cell)
        ws.append(cells)
        count += 1
    ws.auto_filter.ref = f'A1:X{count}'
wb.save(OUT / 'inventory.xlsx')

stamp = dt.datetime.fromisoformat(summary['snapshot_finished_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
table = []
md = ['# Полный каталог oz_acc1 вне CPC', '', f'Снимок: {stamp}. Полгода: с 05.05.2026 00:00 МСК до снимка.', '',
      '**Область: отсутствие в RUNNING CPC.** Глобальная «Оплата за заказ: все товары» включена; эти списки нельзя называть полностью свободными от любого платного продвижения.', '',
      '| Сегмент | Вне CPC | Входило в первую волну | Дополнительно к ней | Собственные базовые проверки пройдены |', '|---|---:|---:|---:|---:|']
for name, label, suffix in segments:
    c = summary['counts'][name]
    first = previous[name]
    table.append(f'<tr><td>{html.escape(label)}</td><td>{fmt(c["count"])}</td><td>{fmt(first)}</td><td>{fmt(c["count"]-first)}</td><td>{fmt(c["own_readiness_passed"])}</td><td>{link(suffix,"Полный CSV")}</td></tr>')
    md.append(f'| {label} | {c["count"]} | {first} | {c["count"]-first} | {c["own_readiness_passed"]} |')
md += [f'| **Итого** | **{summary["nonarchived_outside_running_cpc"]}** | **2953** | **13180** | **{summary["outside_cpc_own_readiness_passed"]}** |', '',
       'Пять сегментов попарно не пересекаются по SKU, product_id и offer_id. Сводный файл — их объединение. Все X2/X6/X10 созданы за полгода, но оставлены только в бандлах; в новинках их повторно нет.', '',
       f'Полный каталог: {summary["catalog_product_ids"]} карточки, из них {summary["nonarchived_catalog_cards"]} неархивных. В RUNNING CPC {summary["nonarchived_in_running_cpc"]} неархивных; вне CPC {summary["nonarchived_outside_running_cpc"]}. Архив вне CPC: {summary["archived_outside_cpc"]}. Общий реестр вне CPC с архивом: {summary["nonarchived_outside_running_cpc"]+summary["archived_outside_cpc"]}.', '',
       'Новизна — product.created_at Seller API, не updated_at и не дата источника FBS/FBO. Бандл — явный суффикс артикула X2/X6/X10. Старые метки подготовительных кампаний не заменяют эти признаки.', '',
       'В прежних 1425 bundles 1345 имеют маркер X2/X6/X10; у 80 маркера нет (все артикулы числовые, явного 2/6/10 в названии нет). Эти 80 отдельно сохранены для сверки: 11 относятся к новым за полгода, 69 — к остальным. Из прежних 1070 old 641 карточка попадает в последние полгода, 429 остаются старше.', '',
       'Полные списки включают отсутствие наличия/ошибки собственной карточки с отдельными флагами; родственники не исключают SKU. Свежие минимальные ставки и бюджеты не проверялись, запуск не разрешён. Экспериментальные назначения не созданы.', '',
       'Количества считаются по основному SKU одной Seller-карточки; дополнительные source SKU сохранены в sku_aliases, реклама проверена по всем алиасам. Историческое «никогда не рекламировался» не устанавливалось: проверено отсутствие в действующем CPC сейчас.', '',
       'CPO: RUNNING ALL_SKU_PROMO 24244024; индивидуальный pool 3302 SKU, searchPromoStatus=false у всех. Это не выключает глобальный режим. По данным 31.10–04.11 у 24244024 есть фактический расход 81 135,60 ₽. Официальный смысл глобального режима: https://seller.ozon.ru/media/news/oplata-za-zakaz-prodvigajte-srazu-vse-tovary-so-stavkoj-5percent-1/', '',
       'Файлы: inventory.xlsx, all_outside_cpc.csv, bundles_x2/x6/x10.csv, new_6months.csv, other_outside_cpc.csv, archived_outside_cpc.csv, all_outside_cpc_including_archive.csv. Доказательства: summary.json, validation.json, read_requests.jsonl, raw/.', '']
(OUT / 'RESULT.md').write_text('\n'.join(md))

page = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>Полный каталог Ozon вне CPC · oz_acc1</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#182230;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1100px;margin:28px auto;padding:32px;background:#fff;border:1px solid #e4e7ec;border-radius:14px}}h1{{font-size:29px;line-height:1.3}}h2{{font-size:22px;margin:30px 0 12px}}a{{color:#175cd3}}.meta{{font-size:14px;color:#667085}}.notice{{padding:18px;background:#fffaeb;border:1px solid #fedf89;border-radius:10px;margin:20px 0}}.downloads{{display:flex;gap:12px;flex-wrap:wrap}}.downloads a{{display:inline-block;padding:10px 16px;border:1px solid #b2ccff;border-radius:8px;text-decoration:none;background:#eff4ff}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:12px;border-bottom:1px solid #e4e7ec;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left;white-space:normal;min-width:170px}}th{{background:#f8fafc}}li{{margin:10px 0}}footer{{margin-top:30px;padding-top:18px;border-top:1px solid #e4e7ec;color:#667085}}@media(max-width:640px){{main{{margin:0;padding:20px 16px;border:0;border-radius:0}}h1{{font-size:25px}}th,td{{padding:9px}}}}@media print{{body{{background:#fff}}main{{border:0;padding:0}}.downloads{{display:none}}}}</style></head><body><main>
<div class="meta">oz_acc1 · {stamp} · полная сверка каталога и действующих кампаний</div>
<h1>Все SKU вне CPC: бандлы, новинки за полгода и остальные</h1>
<p><strong>2953 SKU были только подготовленной первой волной.</strong> Полный неархивный список вне действующего CPC сейчас содержит <strong>{fmt(summary['nonarchived_outside_running_cpc'])} SKU</strong>. Ни одна подготовка не считается запуском.</p>
<div class="notice"><strong>Важное различие рекламы.</strong> В аккаунте работает «Оплата за заказ: все товары», кампания 24244024. Ниже проверено отсутствие в кампаниях <strong>с оплатой за клик (CPC)</strong>. Это не означает отсутствия CPO. Глобальный режим отмечен в каждом файле; его не отменяет searchPromoStatus=false в индивидуальном списке. <a href="https://seller.ozon.ru/media/news/oplata-za-zakaz-prodvigajte-srazu-vse-tovary-so-stavkoj-5percent-1/">Описание Ozon</a>.</div>
<div class="downloads">{link('xlsx','Скачать Excel: все пять групп и архив')}{link('all','Сводный CSV: 16 133 неархивных SKU')}{link('all-with-archive','Все SKU вне CPC, включая архив')}</div>
<h2>Непересекающиеся сегменты</h2>
<p>Приоритет: сначала бандлы, затем небандлы, созданные за полгода, затем остаток. Все пять списков попарно не пересекаются. Сводный файл объединяет их.</p>
<div class="scroll"><table><thead><tr><th>Сегмент</th><th>Вне CPC</th><th>Первая волна</th><th>Дополнительно</th><th>Базовые проверки</th><th>Список</th></tr></thead><tbody>{''.join(table)}
<tr><td><strong>Всего</strong></td><td><strong>16 133</strong></td><td><strong>2 953</strong></td><td><strong>13 180</strong></td><td><strong>{fmt(summary['outside_cpc_own_readiness_passed'])}</strong></td><td></td></tr></tbody></table></div>
<p>«Базовые проверки» — собственная цена RUB, наличие, доступный канал, отсутствие собственных ошибок и архива. Это ещё не допуск к запуску: индивидуальные минимальные ставки и бюджеты на этом шаге не обновлялись. Остальные SKU сохранены в списках с причинами, а не удалены.</p>
<h2>Как проверены полгода и бандлы</h2>
<ul><li>Новинки: <strong>с 5 мая 2026 года, 00:00 МСК, до снимка 5 ноября</strong>. Источник — created_at самой карточки Seller API, не дата обновления.</li>
<li>Бандлы: явный суффикс артикула <strong>X2, X6, X10</strong>, сохранены родительский артикул и количество. В неархивном каталоге всего X2 — 2597, X6 — 2595, X10 — 2596; после вычитания CPC — количества в таблице.</li>
<li>Все <strong>7588</strong> бандлов вне CPC также созданы за полгода. Они оставлены только в бандлах, поэтому в 5342 новинках повторов нет.</li>
<li>Один основной SKU на Seller-карточку. Дополнительные FBS/FBO SKU сохранены; принадлежность рекламе проверена по всем алиасам.</li>
<li>Наличие и ошибки родственника не исключают сам SKU. Карта treatment/control этим расчётом не создавалась.</li></ul>
<h2>Почему прежние группы отличались</h2>
<p>Из прежних <strong>1425 bundles</strong> явный маркер X2/X6/X10 есть у <strong>1345</strong>. У <strong>80</strong> его нет: все артикулы числовые, явного количества 2/6/10 в названии нет. Они отдельно сохранены для проверки прежней метки: 11 вошли в новинки, 69 — в остальные. {link('old-bundle-review','Скачать эти 80 позиций')}.</p>
<p>Из прежних <strong>1070 old</strong> <strong>641</strong> карточка создана за последние полгода и теперь относится к новинкам; <strong>429</strong> старше. Исходные документы первой волны не изменены.</p>
<h2>Полный охват и архив</h2>
<p>Проверено <strong>48 492</strong> карточки: <strong>22 536</strong> неархивных и <strong>25 956</strong> архивных. Из неархивных <strong>6403</strong> находятся в RUNNING CPC, <strong>16 133</strong> — вне CPC. Отдельно в архиве вне CPC <strong>25 893</strong> карточки. Общий реестр вне CPC с архивом: <strong>42 026</strong>.</p>
<p>{link('archive','Скачать архив вне CPC')}. Архив не смешан с пятью сегментами для планирования. Отсутствие CPC сейчас не означает, что SKU никогда раньше не рекламировался.</p>
<h2>Проверки результата</h2><ul><li>Все product_id из обеих выдач Seller API покрыты подробными ответами.</li><li>10 попарных проверок пяти основных групп: пересечений 0.</li><li>Основные SKU и артикулы в сводном неархивном реестре уникальны.</li><li>Текущие составы всех 13 RUNNING CPC-кампаний прочитаны полностью.</li><li>Реклама, ставки, бюджеты, экспериментальные назначения и данные БД не менялись.</li></ul>
<footer>Снимок для планирования, не команда на запуск. {link('data','Сводка JSON')}. <a href="{PREFIX}ozon-wave1-plan-2026-11-05">Сохранённый план первой волны 2953</a>.</footer>
</main></body></html>'''
(OUT / 'index.html').write_text(page)
print('Rendered full inventory BI report, Excel workbook and written result; 5 exclusive segments plus archive.')
