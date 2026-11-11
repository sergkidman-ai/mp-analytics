# поток: mkt
"""Readable owner report plus exactly-20-row detailed HTML; does not fetch or mutate data."""
import pathlib,json,csv,decimal,html,datetime as dt,collections
O=pathlib.Path(__file__).parent;D=decimal.Decimal

def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def load(n):return json.loads((O/n).read_text())
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def num(x):return D(str(x)) if x not in ['',None] else None
def f(x,n=2):return 'нет данных' if x in ['',None] else f'{D(str(x)):,.{n}f}'.replace(',',' ').replace('.',',')
def i(x):return f(x,0)
def pct(x):return f(x)+'%'
def e(x):return html.escape(str(x))
main=read('revised_sku_summary_20.csv');assert len(main)==len({x['sku'] for x in main})==20
ads=read('revised_ad_summary.csv');adidx={(r['scope'],int(r['baseline_days'])):r for r in ads};ac=read('revised_ad_comparison_by_sku.csv');sales=read('revised_sales_windows.csv');sx={(r['scope'],int(r['baseline_days'])):r for r in sales};q=load('revised_quality_summary.json');ex=read('revised_pair_exclusions.csv');evidence=read('revised_wave_candidate_evidence.csv');plan=read('revised_three_stream_plan.csv');cab=read('revised_cabinet_daily.csv');cabidx={r['date_msk']:r for r in cab};post=['2026-10-31','2026-11-01','2026-11-02']
rank=sorted([r for r in ac if r['scope']=='all_available' and r['baseline_days']=='28'],key=lambda r:abs(num(r['delta_spend_rub'])),reverse=True)
lookup={r['sku']:r for r in main}
# Explicit context for the first permitted full wave day, using same-weekday means.
cabday=[]
for n,refs in [(14,['2026-10-19','2026-10-26']),(28,['2026-10-05','2026-10-12','2026-10-19','2026-10-26'])]:
 for metric in ['orders','postings','ordered_value_rub','cpc_reported_spend_rub','cpo_reported_spend_rub']:
  actual=num(cabidx['2026-11-02'][metric]);expect=sum(num(cabidx[d][metric]) for d in refs)/len(refs)
  cabday.append({'date_msk':'2026-11-02','baseline_days':n,'reference_dates':';'.join(refs),'metric':metric,'actual':actual,'expected_same_weekday':expect,'delta':actual-expect,'delta_percent':(actual-expect)/expect*100 if expect else None,'wave_attribution':'none; cabinet context only'})
with (O/'revised_cabinet_20261102_context.csv').open('w',newline='') as file:
 w=csv.DictWriter(file,fieldnames=list(cabday[0]));w.writeheader();w.writerows(cabday)
# Evidence of family overlap and deduplication, separate from per-SKU family projections.
familyq=load('revised_sales_quality.json');ds={}
for n in [14,28]:
 family_sum=sum(num(sx[('family_'+r['sku'],n)]['post_total_ordered_value_rub']) for r in main)
 union=num(sx[('families_union_current',n)]['post_total_ordered_value_rub'])
 ds[str(n)]={'sum_of_20_family_projections_post_value':family_sum,'deduplicated_union_post_value':union,'duplicate_value_excluded':family_sum-union}
save('revised_family_dedup_audit.json',{'current_family_ids':19,'overlapping_target_skus':['866442961','652626682'],'calculation':'union of unique scheme+posting_number+product line; not sum of 20 family projections','post_dedup':ds})
now=dt.datetime.now(dt.timezone.utc).isoformat()
lines=['# Исправленная сверка oz_acc1 по согласованному плану', '',f'Данные и расчёты на 03.11.2026; отчёт сформирован {now}. Исходные выгрузки сохранены. Только чтение API/БД и запись отчётов; реклама, ставки, составы и расписания не менялись.','',
'## 1. Что произошло с 20 SKU после снижения ставок','',
'Основной поштучный отчёт содержит **ровно 20 уникальных SKU**: [читаемая таблица с раскрываемыми подробностями](revised_sku_report_20.html) и [полная таблица CSV](revised_sku_summary_20.csv). В ней есть артикул Seller offer_id, название, все найденные кампании и их текущие ставки, текущие остатки/доступность, рекламные и общие показатели. Исходная ставка и ставка, установленная 30.10, для каждого SKU — **«не восстановлено»**. Удвоение текущей ставки и подмена ставки фактическим CPC не применялись.','',
'После изменения доступны только **3 полных дня: 31.10–02.11 МСК**. Базы: **16–29.10 (14 дней)** и **02–29.10 (28 дней)**. 30.10 исключён как смешанный. Окна 7/14/28 дней после изменения — **ещё не накоплены**.','',
'Расчётное ожидание: `E(metric) = Σ по пост-дням [сумма metric на тех же днях недели базы / число таких дней]`. В 14-дневной базе по 2 субботы, воскресенья и понедельника (6 референсных дней), в 28-дневной — по 4 (12 дней). Ожидаемое среднесуточное = E / 3; фактическое = сумма за 3 дня / 3. CPC = сумма расходов / сумма кликов; ДРР = 100 × сумма расходов / сумма рекламной выручки. Средние дневных CPC/ДРР не использовались.','',
'### Рекламный результат и переносы между кампаниями','',
'| База | Охват | Расход за 3 дня, ₽ | Ожидание, ₽ | Δ, ₽ | Δ, % | Рекламные заказы API: факт / ожидание |','|---|---|---:|---:|---:|---:|---:|']
for n in [14,28]:
 for scope in ['constant_pairs','all_available']:
  r=adidx[(scope,n)];label=f"постоянные {r['constant_pairs']} связок" if scope=='constant_pairs' else 'все доступные кампании, фиксированные 20 SKU'
  lines.append('| '+' | '.join([str(n)+' дней',label,f(r['post_spend_rub']),f(r['expected_spend_rub']),f(r['delta_spend_rub']),f(r['spend_delta_percent']),i(r['post_ad_orders_api'])+' / '+f(r['expected_ad_orders_api'])])+' |')
lines+=['',
'«Все доступные» — сумма каждой уникальной строки SKU × кампания × день ровно один раз по всем полученным CPC-размещениям. Это не только постоянный набор связок и не сумма списков текущего состава. Такая агрегация не теряет переносы между наблюдаемыми кампаниями. Полный исторический охват скрытых/недоступных размещений не доказан. Расход CPO, доступный лишь по кампании, по SKU не распределялся. Рекламные заказы — API orders / «Продано товаров», не уникальные номера общих заказов. Расход с НДС по метрике отчёта, без отдельного разделения денег и бонусов на SKU.','',
'Расход наблюдаемо снизился примерно на 79–81%, рекламные заказы API — с ожидаемых 8,00–8,75 до 3. Причинный эффект ставки этим не установлен. Дневные рекламные показатели до/после, рекламная выручка и ДРР приведены по каждому SKU в основном отчёте. Сырые средние баз разделены с ожиданием по дням недели; при пропуске дня указано число наблюдений, а среднее полного окна оставлено недоступным.','',
'### Общие неотменённые заказы и стоимость заказанного','',
'| Охват | За 3 дня: заказы / стоимость, ₽ | Ожидание по 14 дням: заказы / стоимость, ₽ | Изменение стоимости | Ожидание по 28 дням: заказы / стоимость, ₽ | Изменение стоимости |','|---|---:|---:|---:|---:|---:|']
for scope,label in [('fixed20_exact_sku','Сами фиксированные 20 SKU'),('families_union_current','Объединение текущих 19 групп model_id'),('cabinet','Весь кабинет oz_acc1')]:
 a=sx[(scope,14)];b=sx[(scope,28)]
 lines.append('| '+' | '.join([label,i(a['post_total_orders'])+' / '+f(a['post_total_ordered_value_rub']),f(a['expected_post_orders'])+' / '+f(a['expected_post_ordered_value_rub']),pct(a['delta_percent_ordered_value_rub']),f(b['expected_post_orders'])+' / '+f(b['expected_post_ordered_value_rub']),pct(b['delta_percent_ordered_value_rub'])])+' |')
lines+=['',
'Источник — полные выгрузки FBS/FBO за базу и пост-период, статус отмены на момент чтения. Стоимость заказанного = цена товара в Money RUB × количество; это **не финансовая выручка реализации**. День — in_process_at (при отсутствии created_at), переведённый в МСК. Заказы дедуплицированы по order_id, отправления — по схеме и posting_number. Между датами повторяющихся order_id в полученных неотменённых данных нет.','',
'Для склеек используется **текущий model_info.model_id Seller API на 03.11**, а не восстановленный исторический состав и не предположительная группа по названию. Все SKU с неотменёнными продажами кабинета за выбранные периоды классифицированы через текущие данные Seller. Это 19 групп для 20 целевых SKU; 866442961 и 652626682 имеют общую группу. Семейный итог — объединение строк отправлений, поэтому пересечение не удваивает заказы и стоимость. Товары семейства без продаж и вне целевого списка полностью не перечислялись; они не создают неучтённой стоимости в полной ленте продаж. Исторические переходы между моделями неизвестны; соответствие этой группировки другим внутренним определениям семейств не утверждается.','',
'У самих 20 SKU стоимость заказанного **выше ожидания 28-дневной базы на 13,77%, но ниже ожидания 14-дневной на 16,34%**; число заказов 9 против 12,00/12,50. У текущих семейств стоимость ниже на 7,89%/24,80%. Поэтому устойчивое сохранение общего оборота, как и причинное влияние ставок, **пока не подтверждено**.','',
'### Крупнейшие отклонения SKU по расходам (28-дневная база, все доступные размещения)','',
'| SKU | Артикул | Расход поста, ₽ | Ожидание, ₽ | Δ, ₽ | Δ рекламных заказов API |','|---|---|---:|---:|---:|---:|']
for r in rank[:5]:lines.append('| '+' | '.join([r['sku'],lookup[r['sku']]['article'],f(r['post_total_spend_rub']),f(r['expected_post_spend_rub']),f(r['delta_spend_rub']),f(r['delta_ad_orders_api'])])+' |')
lines+=['',
'### Почему 36 связок, а в прежнем сравнении 28','',
'36 — текущие найденные SKU × кампания, а не 36 товаров. Для прежней 28-дневной базы на 12 нужных референсных и 3 пост-днях полностью представлены 28 связок; все 20 SKU остаются в сравнении через свои наблюдаемые размещения. У остальных 8 связок не хватает хотя бы одной строки. Для 14-дневной базы полный набор на 6+3 нужных датах есть у 31 связки.','',
'| SKU | Кампания | Референсные дни из 12 | Пост-дни из 3 | Расход на имеющихся пост-днях, ₽ |','|---|---|---:|---:|---:|']
for r in ex:
 if r['baseline_days']=='28':lines.append('| '+' | '.join([r['sku'],r['campaign_id'],r['reference_days_reported'],r['post_days_reported'],f(r['reported_post_spend_rub'])])+' |')
lines+=['',
'В доступных данных исключение этих связок не изменяет фактический расход пост-периода; ожидание 28-дневной базы для всех наблюдаемых размещений выше на 3,00 ₽. Для 14-дневной базы оба агрегата совпадают. Это проверка **известных строк**: влияние отсутствующих строк не измерено и нулём не считается. Точный календарь исключений и известные суммы — [revised_pair_exclusions.csv](revised_pair_exclusions.csv).','',
'Качество: 638/640 — наличие хотя бы одной строки SKU × день, **не полнота всех исторических размещений**. Нет строк 1510947813 за 29.10 и 4489633315 за 02.10; адресные повторные запросы этих дат пробелы не закрыли. По 9 архивным кампаниям состав недоступен (HTTP 400); их ID были включены в прежние запросы статистики по всем 31 SKU-кампаниям, но пустой отчёт и неизвестный исторический состав не доказывают нулевого участия. Текущий состав кампаний историческим не считался.','',
'**Решение для владельца:** данных достаточно зафиксировать наблюдаемое сокращение рекламных расходов. Для решения о масштабировании на основании причинного эффекта данных недостаточно: только 3 дня, чувствительность общих продаж к базе, поздняя атрибуция/отмены, нет подтверждённой истории ставок и исторической доступности. **Следующее действие:** 07.11 снять 7 полных дней 31.10–06.11 тем же фиксированным составом 20 SKU, обеими базами и дедупликацией семей; до этого восстановить журнал установленных 30.10 ставок. Автоматизация и изменение рекламы этим отчётом не назначались.','',
'## 2. Что реально запущено из трёх потоков и какая часть плана восстановлена','',
'По сообщённому владельцем плану первая волна разрешена на 02–08.11. Immutable manifest — **2963 SKU**, SHA256 `54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2`. Исходный файл с этим хешем среди 323 проверенных локальных файлов не найден: соответствие immutable manifest не проверено. Десять исключённых при подготовке SKU и их причины не восстановлены.','',
'Выгрузками подтверждён текущий подготовленный состав **2953 уникальных SKU без пересечений потоков: 458 new / 1425 bundles / 1070 old**. Разделение основано на заданном владельцем соответствии подготовительных кампаний потокам и их фактическом составе, а не на догадках по названиям товаров. Технический недельный бюджет трёх подготовительных кампаний суммарно **5 906 000 ₽**, это не план расхода. **12 500 ₽** — ориентир фактических расходов волны; сравнение с ним невозможно без подтверждённого состава работающей волны. Индивидуальный floor обязателен, но его индивидуальные значения и факт соблюдения из полученных источников не восстановлены.','',
'| Поток | Всего планировалось | Первая неделя: подготовлено | Фактически подтверждено | Остаток и следующие недели | Источник |','|---|---|---:|---|---|---|']
for r in plan:lines.append('| '+' | '.join([r['stream'],r['total_planned'],r['first_week_prepared_count'],r['factually_confirmed'],r['remaining_to_confirmed_launch']+'; даты и объёмы следующих волн не восстановлены','план владельца + свежий состав подготовительных кампаний'])+' |')
lines+=['',
'Для бандлов сохранено требование ввода всего согласованного объёма частями, включая продающиеся и непродающиеся товары; его полное численное значение недоступно. Для старых позиций вне рекламы сохранён отдельный поток частичного ввода. Остаток к запуску не вычислялся из подготовленных позиций, поскольку факт запуска этих позиций ещё не установлен. Произвольного деления по неделям и придуманных дат нет.','',
'### Раздельная проверка создания, активации, работы и текущего состава','',
'| Кампания | Роль | Создана | Текущий статус | Активация / показы или расход | SKU сейчас | Связь с подготовленными 2953 |','|---|---|---|---|---|---:|---|']
for r in evidence:
 activation='показы в датах 02–03.11; момент активации неизвестен' if r['activity_in_wave_dates_observed']=='True' else ('работа до 02.11 подтверждена; в волну нет данных' if r['activity_before_wave']=='True' else 'не подтверждена; статистика отсутствует')
 lines.append('| '+' | '.join([r['campaign_id'],r['role'],'да, '+r['created_at_utc'][:10],r['state_now'].replace('CAMPAIGN_STATE_',''),activation,r['current_sku_count'],r['overlap_with_prepared_2953']+' SKU пересечения; принадлежность волне не подтверждена'])+' |')
lines+=['',
'**49321871:** автоматическая CPC-кампания «Бонусы на продвижение товаров-новинок FBS», создана 02.11 в 17:03:53 МСК; RUNNING, MAX_CLICKS, fromDate=02.11, в текущем составе **1 SKU 6201364326**. Факт активации/работы подтверждается доставленными показами: 02.11 — 20, 03.11 (неполный снимок) — 17; кликов и расходов в этих строках 0, рекламная метрика orders=0, CPC не определён. День 02.11 неполный. SKU **не входит ни в один из подготовленных 2953**, поэтому его категория потока оставлена **unknown**. Название «новинки» не использовалось для отнесения к new. По полной ленте неотменённых отправлений за 02.11 продаж этого SKU нет.','',
'Дополнительно обоснованно проверены более старые кампании 37536223, 40385357, 42756090: в их свежих составах есть соответственно 1/2/1 SKU из подготовленного new. Все сейчас INACTIVE; у 42756090 есть работа до волны. Пересечение состава не доказывает запуск согласованной волны. По сохранённым отчётам на 02.11 положительной активности подготовленных SKU не найдено; это **не доказательство неактивации или незапуска**, поскольку история событий и полный исторический состав отсутствуют.','',
'Подготовительные 49173478/49173499/49173502 действительно созданы и содержат 458/1425/1070 товаров, сейчас INACTIVE; точная история активации не получена. fromDate=01.11 — настройка, не свидетельство фактического старта. Фактические составы всех 7 проверенных кампаний сохранены, неизвестная категория отделена.','',
'**Установленный статус плана:** численно и по текущему SKU-составу восстановлена подготовка первой недели. Подтверждена работа автоматической кампании с одним товаром вне подготовленного списка. Принадлежность работающих размещений согласованной первой волне остаётся неустановленной; суммарный результат волны и её косвенный эффект не объявляются.','',
'### Контекст кабинета; не результат первой волны','',
'| Показатель за 02.11 | Факт | Ожидание по понедельникам 14-дневной базы | Ожидание по понедельникам 28-дневной базы |','|---|---:|---:|---:|']
labels={'orders':'Неотменённые общие заказы','postings':'Неотменённые отправления','ordered_value_rub':'Стоимость заказанного, ₽','cpc_reported_spend_rub':'Доступный расход CPC, ₽','cpo_reported_spend_rub':'Доступный расход оплаты за заказ, ₽'}
for metric,label in labels.items():
 a=next(r for r in cabday if r['baseline_days']==14 and r['metric']==metric);b=next(r for r in cabday if r['baseline_days']==28 and r['metric']==metric)
 lines.append('| '+' | '.join([label,f(a['actual'],0 if metric in ['orders','postings'] else 2),f(a['expected_same_weekday']),f(b['expected_same_weekday'])])+' |')
lines+=['',
'Это показатели всего кабинета, а не волны и не «органика». CPO взят из фактической кампанийной метрики moneySpent по PaymentType=CPO; 5% и другие ставки не предполагались. Рекламные заказы из общих заказов не вычитались. За 3 пост-дня стоимость заказанного кабинета ниже ожидания базы 14/28 дней на 12,74%/18,93%; за отдельный понедельник 02.11 — выше на 4,89% относительно 14-дневной и ниже на 1,73% относительно 28-дневной. Это наблюдения, не поправочный коэффициент «ноябрьского спада» и не причинный эффект рекламы.','',
'**Следующее действие по запуску:** получить исходный файл 2963 SKU с указанным SHA256 и журнал запуска/финальный список кампаний первой волны, сверить его с сохранёнными 2953 SKU и индивидуальными floor. По подтверждённым ID привязать поток и считать общие продажи/семейства и расходы; до подтверждения сохранить unknown, не менять кампании и не переносить технический бюджет в план расхода.','',
'## Примечание о качестве данных и точные файлы','',
'- Основной отчёт ровно по 20 SKU: [revised_sku_report_20.html](revised_sku_report_20.html), [revised_sku_summary_20.csv](revised_sku_summary_20.csv). В нём раскрыты среднесуточные показатели, CPC/ДРР, полные и наблюдаемые дни, обе базы, текущие ставки и остатки, продажи SKU и его текущей группы.',
'- Отдельные детали: [revised_sku_campaign_daily.csv](revised_sku_campaign_daily.csv) — 987 уникальных исходных строк; [revised_ad_comparison_by_sku.csv](revised_ad_comparison_by_sku.csv), [revised_ad_summary.csv](revised_ad_summary.csv), [revised_pair_exclusions.csv](revised_pair_exclusions.csv), [revised_sku_day_coverage.csv](revised_sku_day_coverage.csv), [revised_comparison_design.json](revised_comparison_design.json).',
'- Общие продажи: [revised_sales_daily.csv](revised_sales_daily.csv), [revised_sales_windows.csv](revised_sales_windows.csv), [revised_sales_lines.csv](revised_sales_lines.csv), [revised_family_members.csv](revised_family_members.csv), [revised_family_dedup_audit.json](revised_family_dedup_audit.json), [revised_sales_quality.json](revised_sales_quality.json). Исходные FBS/FBO и сведения о товарах — `revision_raw/postings_*.json`, `revision_raw/product_info_*.json`.',
'- План: [revised_three_stream_plan.csv](revised_three_stream_plan.csv), [revised_plan_reference.json](revised_plan_reference.json), [revised_prepared_composition_2953.csv](revised_prepared_composition_2953.csv), [revised_wave_candidate_evidence.csv](revised_wave_candidate_evidence.csv), [revised_wave_candidate_composition.csv](revised_wave_candidate_composition.csv), [revised_prepared_ad_context.csv](revised_prepared_ad_context.csv).',
'- Кабинет: [revised_cabinet_daily.csv](revised_cabinet_daily.csv), [revised_cabinet_spend_comparison.csv](revised_cabinet_spend_comparison.csv), [revised_cabinet_20261102_context.csv](revised_cabinet_20261102_context.csv).',
'- Все файлы находятся в `/opt/mp-analytics/docs/reports/ozon_recovery_20261103/`. Старые `raw/`, исходные CSV и прежний README сохранены. Новые запросы сделаны только для отсутствовавших артикулов/остатков/моделей, общих продаж, текущих обоснованных кандидатов и двух конкретных пробелов SKU × день. Новая полная рекламная выгрузка не заказывалась.',
'- Задержка атрибуции рекламных заказов и последующие отмены могут изменить короткий пост-период. Текущая доступность не доказывает доступность в прошлом. Нет подтверждённой истории ставок, индивидуальных floor, исходного immutable manifest и полной истории составов. Контрольные суммы дополнений — `REVISED_SHA256SUMS`; проверки — `revised_validation.json`. Финансовая приемлемость расходов не объявляется установленной: требуются согласованный порог и подтверждённая экономика; косвенный прирост не оценён причинно.','']
(O/'CORRECTED_REPORT.md').write_text('\n'.join(lines))
# Exactly 20 main HTML rows. Per-row details expose the complete before/after report without repeated SKU rows.
htmlrows=[]
metriclabels={'views':'Показы / день','clicks':'Клики / день','spend_rub':'Расход, ₽ / день','ad_orders_api':'Рекламные заказы API / день','ad_revenue_rub':'Рекламная выручка, ₽ / день'}
for r in main:
 blocks=[]
 for n in [14,28]:
  pref=f'ad_all_base{n}_';aa={k[len(pref):]:v for k,v in r.items() if k.startswith(pref)}
  trs=[]
  for m,label in metriclabels.items():trs.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [label,f(aa['base_mean_observed_day_'+m]),f(aa['expected_daily_'+m]),f(aa['post_daily_'+m])])+'</tr>')
  for rate,label in [('cpc_rub','CPC, ₽ (расход/клики)'),('drr_percent','ДРР, % (расход/рекл. выручка)')]:trs.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [label,f(aa['base_reported_total_'+rate]),f(aa['expected_post_'+rate]),f(aa['post_total_'+rate])])+'</tr>')
  blocks.append(f'<h4>База {n} дней; наблюдаемые дни {aa["base_days_observed"]}/{n}, пост {aa["post_days_observed"]}/3</h4><table><tr><th>Показатель</th><th>Среднее базы по наблюдаемым дням</th><th>Ожидание того же дня недели / день</th><th>После / день</th></tr>'+''.join(trs)+'</table>')
 saleblocks=[]
 for n in [14,28]:
  for scope,label in [('self','Сам SKU'),('family_current','Текущая группа model_id')]:
   pref=f'sales_{scope}_base{n}_';sr={k[len(pref):]:v for k,v in r.items() if k.startswith(pref)}
   trs=[]
   for metric,label2 in [('orders','Общие неотменённые заказы'),('postings','Отправления'),('units','Товарные единицы'),('ordered_value_rub','Стоимость заказанного, ₽')]:trs.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [label2,f(sr['base_daily_'+metric]),f(sr['expected_daily_'+metric]),f(sr['post_daily_'+metric]),f(sr['post_total_'+metric])])+'</tr>')
   saleblocks.append(f'<h4>{label}, база {n} дней</h4><table><tr><th>Показатель</th><th>База / день</th><th>Ожидание / день</th><th>Пост / день</th><th>За 3 дня</th></tr>'+''.join(trs)+'</table>')
 a28=next(x for x in ac if x['sku']==r['sku'] and x['scope']=='all_available' and x['baseline_days']=='28')
 stock=json.loads(r['stock_by_source']);avail=json.loads(r['availability_by_source'])
 stocktext='; '.join(f"{x.get('source')}: present={x.get('present')}, reserved={x.get('reserved')}" for x in stock)
 availtext='; '.join(f"{x.get('source')}: {x.get('availability')}" for x in avail)
 detail='<details><summary>Рекламные показатели до/после</summary>'+''.join(blocks)+'</details><details><summary>Общие продажи SKU и текущей группы</summary>'+''.join(saleblocks)+'</details>'
 detail+='<details><summary>Ставки, остатки, ограничения</summary><p>'+e(r['campaigns_and_current_bids'])+'</p><p>Исходная ставка: не восстановлено. Установленная 30 октября: не восстановлено. Ставка и фактический CPC — разные показатели.</p><p>'+e(stocktext)+'</p><p>'+e(availtext)+'</p><p>Текущий model_id: '+e(r['current_family_model_id'])+'; count API: '+e(r['current_family_reported_count'])+'. Исторический состав/остаток неизвестен. Срез '+e(r['stock_snapshot_utc'])+'. Значения present/reserved показаны как в API, свободный остаток вычитанием не выводился.</p></details>'
 htmlrows.append('<tr class="sku"><td>'+e(r['sku'])+'</td><td>'+e(r['article'])+'<br>'+e(r['name'])+'</td><td>'+e(f(a28['post_total_spend_rub']))+' ₽<br>Δ '+e(f(a28['delta_spend_rub']))+' ₽ ('+e(f(a28['spend_delta_percent']))+'%)</td><td>'+e(i(r['sales_self_base28_post_total_orders']))+' / '+e(f(r['sales_self_base28_post_total_ordered_value_rub']))+' ₽</td><td>'+detail+'</td></tr>')
page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>oz_acc1 — ровно 20 SKU, исправленный отчёт</title><style>body{font:15px/1.5 system-ui,sans-serif;color:#17212d;background:#f6f7f9;margin:20px}main{max-width:1600px;margin:auto}table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #d7dde5;padding:9px;vertical-align:top;text-align:left}th{background:#eaf0f7}details{margin:8px 0}summary{cursor:pointer;font-weight:600}details table{font-size:13px}h1{font-size:24px}p{max-width:1100px}.sku>td:nth-child(2){min-width:230px}.sku>td:last-child{width:50%}input{padding:8px;width:360px;max-width:90%}@media print{body{margin:0;background:white}input{display:none}details{break-inside:avoid}}</style><main><h1>Фиксированные 20 SKU oz_acc1</h1><p>Базы: 16–29 октября и 2–29 октября. После: 31 октября – 2 ноября, 3 полных дня МСК. 30 октября исключён. Суммы расходов показаны по всем доступным уникальным SKU × кампания × день, а не только по постоянным связкам. Основная таблица имеет ровно 20 строк. <a href="CORRECTED_REPORT.md">Общий исправленный отчёт</a> · <a href="revised_sku_summary_20.csv">Все поля CSV</a> · <a href="revised_pair_exclusions.csv">Исключения постоянного набора</a></p><p>Клики/показы/деньги/заказы и выручка усредняются по указанному числу наблюдаемых дней; полное календарное среднее при пропуске не заявляется. CPC и ДРР — отношения сумм, не ставки. Общая стоимость заказанного — Money RUB × количество, не финансовая реализация. Семейства — текущий model_id на 3 ноября; их общий итог дедуплицирован отдельно. Отсутствующая строка не равна нулю. Окна поста 7/14/28 дней ещё не накоплены. Короткий пост может измениться из-за атрибуции и отмен.</p><input id="filter" placeholder="Найти SKU, артикул или название"><p id="count">20 уникальных SKU</p><table><thead><tr><th>SKU</th><th>Артикул и название</th><th>Реклама за 3 дня; Δ к 28-дневной базе</th><th>Общие заказы / стоимость SKU за 3 дня</th><th>Подробности</th></tr></thead><tbody>'''+''.join(htmlrows)+'''</tbody></table></main><script>document.querySelector('#filter').addEventListener('input',function(){let n=0;document.querySelectorAll('tr.sku').forEach(r=>{const show=r.textContent.toLowerCase().includes(this.value.toLowerCase());r.hidden=!show;if(show)n++});document.querySelector('#count').textContent=n+' из 20 SKU'});</script></html>'''
assert page.count('<tr class="sku">')==20
(O/'revised_sku_report_20.html').write_text(page)
print('Written CORRECTED_REPORT.md and detailed HTML with 20 unique rows')
print('Family dedup audit',ds['28'])
