# поток: mkt / ozon
"""Render V3 plan and family/rating findings to the existing protected BI service."""
import csv
import html
import json
from pathlib import Path

OUT = Path(__file__).parent
BASE = '/api/wb-bids/reports/'
KEY = 'ozon-waves-2026-11-06'
m = json.loads((OUT / 'wave_draft_manifest.json').read_text())
f = m['child_family_summary']
cost = json.loads((OUT / 'cost_preview.json').read_text())


def esc(s):
    return html.escape(str(s))


def link(suffix, label):
    return f'<a href="{BASE}{KEY}{suffix}">{esc(label)}</a>'


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(s)}</th>' for s in headers) + \
        '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{s}</td>' for s in row) + '</tr>' for row in rows) + '</tbody></table></div>'


labels = [('new_6months', 'Новинки'), ('bundles_x2', 'Бандлы X2'), ('bundles_x6', 'Бандлы X6'),
          ('bundles_x10', 'Бандлы X10'), ('other_outside_cpc', 'Старые'), ('children', 'Дочерние')]
wave_table = []
for key, label in labels:
    values = [w['segments'].get(key, 0) for w in m['wave_summary']]
    wave_table.append([esc(label), *map(str, values), str(sum(values))])
wave_table += [['<strong>Всего SKU</strong>', *[f"<strong>{w['planned_skus']}</strong>" for w in m['wave_summary']], '<strong>9242</strong>'],
               ['Семей дочерних', *[str(w['children_families']) for w in m['wave_summary']], '1043']]
campaign_table = [[str(c['wave']), esc(c['title']), str(c['planned_skus']),
                   link('-campaign-' + c['key'], 'Состав CSV'), 'Не создана'] for c in m['campaigns']]
family_sizes = [[str(n), str(count), str(int(n) * count)] for n, count in f['family_size_distribution'].items()]
budgets = ' → '.join(f'{n:,}'.replace(',', ' ') for n in cost['cumulative_weekly_native_minimum_preview_rub'])
body = f'''
<div class="meta">oz_acc1 · редакция от 06.11.2026 · исходный снимок наличия 05.11.2026, 12:33 МСК</div>
<h1>Три волны: X2 → X6 → X10, отдельные РК по группам</h1>
<div class="notice"><strong>Составы и названия подготовлены. Новые РК в Ozon не созданы и не активированы.</strong>
Не заданы бюджеты и предельный расход, индивидуальные минимальные ставки не обновлены.
Автостоп не установлен. Даты фактического старта и остановки отсутствуют.</div>
<p>Во всех списках только исходные 9242 SKU в наличии вне CPC: новинки, бандлы,
старые и дочерние. Это сохранённый снимок, не повторная проверка наличия на 6 ноября.
Общий CPO уже действовал: измеряется добавление CPC поверх CPO.</p>
<h2>Распределение по трём волнам</h2>
<p>Волна 1 — D0, волна 2 — D0+7 дней, волна 3 — D0+14 дней. Среди бандлов
в первой только X2, во второй только X6, в третьей только X10.</p>
{table(['Группа', 'Волна 1', 'Волна 2', 'Волна 3', 'Всего'], wave_table)}
<div class="links">{link('-wave1','Волна 1 — CSV')} {link('-wave2','Волна 2 — CSV')}
{link('-wave3','Волна 3 — CSV')} {link('-all','Все SKU — CSV')}</div>
<h2>12 отдельных рекламных кампаний</h2>
<p>На каждую волну — четыре РК: новинки, бандлы своего размера, старые и дочерние.
Метка «Волна 1/2/3» входит в название каждой РК. Кампании ниже — подготовленные
составы, без фактических ID Ozon; при чтении API на 6 ноября совпадений названий нет.</p>
{table(['Волна', 'Название РК', 'SKU', 'Скачать', 'Состояние'], campaign_table)}
<p>{link('-campaigns','Сводка всех 12 РК — CSV')} · {link('-data','Manifest V3 — JSON')}</p>
<h2>3252 дочерних SKU — это 1043 семьи</h2>
<p>Семья здесь — <strong>один родительский артикул из четырёх цифр и все дочерние
артикулы с этим префиксом в нашем списке 3252</strong>. Это артикульная семья,
она отличается от склейки model_id Ozon.</p>
<ul><li>383 семьи имеют одну дочернюю карточку в фиксированном списке.</li>
<li>660 семей имеют две и более дочерних; максимум — 51.</li>
<li>В среднем 3,12 дочерней карточки на родителя. Это число в выбранном пуле,
а не утверждение о полном количестве всех дочерних в каталоге.</li>
<li>Всего 1043 родителя и 3252 дочерних — 4295 связанных карточек при таком подсчёте.
Это не увеличение рекламного пула: он остаётся 9242 SKU.</li>
<li>По последнему уточнению <strong>родителей дополнительно не ищем для включения,
не добавляем и не привязываем к волне дочерних</strong>. Все дочерние одной семьи
попадают в одну волну; родители, уже входящие в новинки/старые, распределяются отдельно.</li>
<li>В сохранённых данных родители известны у всех 1043 семей; у 27 семей
родитель в архиве (130 дочерних SKU). Это не исключает доступных дочерних.</li></ul>
<div class="links">{link('-families','1043 семьи — полный CSV')}
{link('-family-sizes','Размеры семей — CSV')}</div>
<details><summary>Распределение семей по числу доступных дочерних в списке</summary>
{table(['Дочерних в семье', 'Семей', 'Дочерних SKU'], family_sizes)}</details>
<h2>Много ли дочерних с рейтингом?</h2>
<p><strong>По текущим данным ответить достоверно нельзя: актуальный рейтинг неизвестен
для всех 3252 дочерних, это не означает «у всех нет рейтинга».</strong></p>
<p>В новой БД ozon_rating нет строк oz_acc1; в сохранённых product-info нет звёздного
рейтинга и числа отзывов. Канал отзывов отключён в проекте после потери доступа по
подписке; его заново не запускали. Подключённой сессии браузера/документа нет.</p>
<p>Есть частичный архив от 14.09.2026: он покрывает 40 дочерних из 3252. Отдельно
найдены 29 родителей, у 9 тогда был положительный рейтинг. Родительский рейтинг
не переносится на дочерний и этот старый срез не считается текущим.</p>
<p>Для точного ответа нужна текущая выгрузка из ЛК Ozon: SKU или артикул, звёздный
рейтинг, число отзывов, дата выгрузки. После сопоставления можно посчитать долю
дочерних со звёздами, уровни рейтинга и семьи с оценёнными дочерними отдельно от родителей.</p>
<p>{link('-ratings','Покрытие рейтингов всех 3252 SKU — CSV (UNKNOWN отдельно от нуля)')}</p>
<h2>Как учитывать семьи и склейки в измерении</h2>
<p>Дочерние одной артикульной семьи не разделены по волнам. Для небандлов также
сохранены связанные model_id. Но правило размеров бандлов вынужденно разводит
<strong>{m['models_forced_to_multiple_waves_by_bundle_sizes']} склеек Ozon</strong>
по разным неделям. Всего в новом плане несколько волн у {m['model_ids_actually_in_multiple_waves']}
model_id, включая связи с небандлами. Прежнее утверждение «каждый model_id только
в одной волне» к V3 не относится.</p>
<p>По таким склейкам измеряется накопление воздействия X2 → X6 → X10; их нельзя
считать независимыми контрольными группами. Ноябрьские T/C не назначены,
исторические E5/E8 не менялись. {link('-models','Контекст волн по model_id — CSV')}.</p>
<h2>Ежедневный замер и месяц наблюдения</h2>
<ul><li>SKU: рекламные показы, клики, расход, CPC, атрибутированные продажи и ДРР;
отдельно все заказы и стоимость заказанного.</li>
<li>Артикульные семьи и склейки Ozon: все заказы и стоимость полного объединения,
включая соседние карточки; уникальные заказы без суммирования дублей.</li>
<li>Аккаунт — приоритет: общий рекламный расход CPC/CPO, все заказы, стоимость
заказанного, отмены и задержки. Стоимость заказанного не финансовая выручка или прибыль.</li>
<li>На фактическом входе — собственные наличие/допуски и индивидуальный min/sku;
ставка по ответу каждого SKU, без ежедневного разгона.</li></ul>
<p>При общем месяце от первой волны — примерно 30/23/16 дней воздействия. Если
нужен месяц каждой волне, общий срок около шести недель; этот выбор пока открыт.
Постоянный контроль 15% предложен, но не назначен и в CSV не вычтен.</p>
<p>Наблюдение прежних 20 SKU и аккаунта до 10 ноября — отдельная задача;
ставки по ней не меняются. Новые массовые включения могут повлиять на этот замер.</p>
<h2>Бюджет и создание РК</h2>
<p>Максимальный дополнительный расход CPC за месяц пока не задан. Создание требует
настройки недельного бюджета, в payload стоит null; минимальный CPC не ограничивает
общую сумму. По <a href="https://docs.ozon.kz/performance/product-ads/">опубликованному
правилу Ozon Performance</a> минимум 2000 ₽/SKU в неделю даёт для полного пула при
накоплении волн {budgets} ₽ технического недельного бюджета.
Это <strong>не прогноз списаний, не согласованный расход</strong>; применимость к
этому аккаунту и возможность обеспечить разрешённый лимит проверяются до записи.</p>
<p>Следующий шаг — задать разрешённый расход/бюджеты и отсчёт месяца, обновить
собственные допуски и индивидуальные floor, затем создать и перечитать 12 неактивных
РК. Перед активацией — завершить выбор контроля и установить/проверить автостоп.
Выполненная подготовка не выдаётся за создание или запуск.</p>
<p><a href="{BASE}ozon-year-review-2026-11-05">Отдельный годовой разбор длинного хвоста</a>
· {link('-cost','Предварительные бюджетные ограничения — JSON')}</p>
'''
css = '''*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#182230;font:17px/1.65 system-ui,sans-serif}main{max-width:1150px;margin:28px auto;padding:32px;background:white;border:1px solid #e4e7ec;border-radius:14px}h1{font-size:29px;line-height:1.3}h2{font-size:23px;margin-top:32px}a{color:#175cd3}p,li{overflow-wrap:anywhere}li{margin:9px 0}.meta{color:#667085;font-size:14px}.notice{padding:17px;border:1px solid #fedf89;background:#fffaeb;border-radius:10px;margin:22px 0}.links{display:flex;gap:12px;flex-wrap:wrap}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;margin:16px 0}th,td{padding:11px;border-bottom:1px solid #e4e7ec;text-align:left}th{background:#f8fafc}details{margin:18px 0}summary{cursor:pointer}@media(max-width:640px){main{margin:0;padding:20px 16px;border:0;border-radius:0}h1{font-size:25px}th,td{padding:8px}}'''
(OUT / 'index.html').write_text('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<meta name="robots" content="noindex,nofollow,noarchive"><title>Ozon: три волны и 1043 семьи дочерних</title>'
    '<style>' + css + '</style></head><body><main>' + body + '</main></body></html>')

# Join existing historical flags onto the NEW waves without repeating annual collection.
old = OUT.parent / 'ozon_monthly_experiment_20261105/year_flags_for_wave_draft.csv'
with old.open(encoding='utf-8-sig') as handle:
    history = {r['sku']: r for r in csv.DictReader(handle)}
with (OUT / 'wave_draft_all_candidates.csv').open(encoding='utf-8-sig') as handle:
    joined = []
    for r in csv.DictReader(handle):
        joined.append({**history[r['sku']], 'draft_wave': r['draft_wave'], 'segment': r['segment'],
                       'parent_same_wave_required': False})
with (OUT / 'year_flags_for_wave_draft.csv').open('w', encoding='utf-8-sig', newline='') as handle:
    writer = csv.DictWriter(handle, fieldnames=list(joined[0])); writer.writeheader(); writer.writerows(joined)
print('Rendered V3 report, 12 campaign previews, family/rating downloads and updated historical joins.')
