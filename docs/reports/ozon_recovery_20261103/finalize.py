# поток: mkt
"""Build the human report and provenance from actual one-off exports."""
import pathlib,json,csv,datetime as dt,collections,hashlib,subprocess,decimal
O=pathlib.Path(__file__).parent;P=pathlib.Path('/opt/mp-analytics');D=decimal.Decimal
now=dt.datetime.now(dt.timezone.utc).isoformat()
def load(n):return json.loads((O/n).read_text())
def rows(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def fmt(x,d=2):
 if x is None or x=='':return 'нет данных'
 return f'{D(str(x)):,.{d}f}'.replace(',',' ').replace('.',',')
def count(x):return fmt(x,0)
cov=load('coverage_summary.json');comp=load('comparison_summary.json');pre=load('preflight.json');wave=load('wave_assessment.json');candidates=rows('wave_candidates.csv');nov=rows('sku_20261102.csv');largest=rows('largest_deviations.csv');byday=rows('comparison_by_day.csv')
novsp=sum(D(r['spend_rub']) for r in nov if r['spend_rub']);novclick=sum(D(r['clicks']) for r in nov if r['clicks']);novview=sum(D(r['views']) for r in nov if r['views']);novord=sum(D(r['ad_orders_api']) for r in nov if r['ad_orders_api'])
targets='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split()
assert {r['sku'] for r in nov}==set(targets) and all(r['status']=='reported' for r in nov)
sourcefiles=['AGENTS.md','CLAUDE.md','.claude/rules/ozon.md','.claude/rules/data-layer.md','collectors/ozon_ads.py','collectors/ozon_bids.py','collectors/ozon.py','tools/ozon_bid_ramp.py']
save('sources.json',{'completed_at_utc':now,'account':'oz_acc1','host':'cloud-001','project':str(P),'actual_report_directory':str(O.resolve()),'permission_mode':pre['permission_mode'],'existing_client_sources':[{'path':str(P/n),'sha256':hashlib.sha256((P/n).read_bytes()).hexdigest()} for n in sourcefiles],'git_head':subprocess.run(['git','-C',str(P),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip(),'raw_campaign_metadata':'raw/campaigns.json','raw_products':'raw/products_<campaign_id>.json','raw_campaign_daily':'raw/daily_<from>_<to>_<batch>.json','raw_sku_json':'raw/sku_report_<from>_<to>_<batch>_result.json','raw_sku_csv_archives':['raw/csv_2026-10-02_2026-10-31.zip','raw/csv_2026-11-01_2026-11-03.zip'],'raw_sku_csv_files':'raw/csv_<from>_<to>/*.csv','local_manifest_search':'manifest_search.json','db_sources':'db_schema.json; db_tables.json; SELECT-only inspection','unavailable_sources':['final November CPC wave manifest','initial 20-SKU bid-change manifest','activation history via existing project clients','historical advertising DB tables: ozon_bids, mkt_ozon_ads_sku_daily, mkt_ozon_bid_journal, mkt_ozon_bid_step_log are absent from restored DB','9 archived campaign compositions: HTTP 400','confirmed family composition and associated sales'],'old_session_artifacts':'prior_restricted_session/ (historical blocked attempts; not current status)'})
requests=[]
for line in (O/'api_requests.jsonl').read_text().splitlines():requests.append(json.loads(line))
allowedpost={'/api/client/token','/api/client/statistics/json','/api/client/statistics'}
assert all(r['method']=='GET' or (r['method']=='POST' and r['path'] in allowedpost) for r in requests)
save('audit_summary.json',{'completed_at_utc':now,'account':'oz_acc1','permission_mode':pre['permission_mode'],'filesystem_create_delete_test':pre['filesystem'],'seller_read_check':pre['seller_api'],'performance_access_check':pre['performance_api'],'database_select1':pre['database'],'performance_request_count_logged':len(requests),'http_status_counts':dict(collections.Counter(str(r.get('http')) for r in requests)),'campaign_mutations':0,'bid_mutations':0,'budget_mutations':0,'composition_mutations':0,'status_mutations':0,'schedule_mutations':0,'database_writes':0,'readonly_guard':'acquire.py: only GET; POST only token and statistics report generation','token_or_secret_values_saved':False,'acquisition_complete':True,'wave_membership_confirmed':False,'bid_rollback_confirmed':False,'family_total_orders_computed':False})
method='''# Методика и ограничения

Все запросы — только oz_acc1. Использованы существующие клиенты `collectors.ozon_ads` (_token, PERF), `collectors.ozon_bids._campaign_products`, `collectors.ozon._headers`, `core.db.get_conn`. Разовые обвязки лежат рядом с отчётом, штатный сборщик Beget не восстанавливался и его результаты не подменялись.

## Даты и единицы

Даты метаданных `createdAt` переведены из UTC в Europe/Moscow. Окно кандидатов — создание 01–03.11.2026 по МСК, по фактически выполненному снимку 03.11, не весь будущий остаток 3 ноября. `fromDate` — настроенная дата начала, не доказательство активации. Точное время активации не получено. За 3 ноября выгружен дополнительный неполный текущий день; в сравнении он не участвует.

Статистика запрошена по календарным датам `dateFrom`/`dateTo`; возвращённые дневные даты используются как календарные дни отчёта Ozon по МСК. В отчётах DATE фактически присутствуют день и SKU. Запрошены 02–31.10 и 01–03.11, основной ряд отфильтрован до 02.11 включительно. Значения не распределяются из итогов кампании на SKU.

Расход в основной SKU-выгрузке — колонка CSV «Расход, ₽, с НДС» (JSON moneySpent). Это рекламная метрика Ozon; денежные списания, бонусы и абонентский счёт на уровне SKU отдельно не разделялись. CPC = расход / клики, при нуле кликов не определён. «Рекламные заказы» в таблицах — метрика API orders, соответствующая колонке «Продано товаров»; уникальные номера заказов не доступны, число уникальных заказов этим не подтверждается. Заказы моделей, семейства, склейки не суммировались.

## Сырые данные и пропуски

Получены все 48 кампаний, возвращённые API, без фильтра по текущему состоянию. Для всех 31 кампании типа SKU запрошены дневные JSON-отчёты двумя окнами, партиями до 10. Для шести кампаний с текущими связками заданных SKU получены CSV/ZIP двумя окнами. Остальные кампании проверены JSON-отчётами на исторические строки заданных SKU: дополнительных связок вне CSV не найдено.

В JSON числовые нули некоторых целочисленных метрик опускаются сервером. Основная SKU-таблица построена из CSV с явными нулями; 1017 строк заданных SKU, включая дополнительный 3 ноября, сверены с JSON по пяти метрикам, расхождений нет. Пропущенные поля JSON не использовались как источник подставленных нулей. Большие ответы API с gzip-телом сохранены также в исходном сжатом виде и разобраны локально. Отсутствующая строка и пустой отчёт всегда «нет данных».

По 9 архивным кампаниям состав недоступен: API возвращает HTTP 400 «Товары перенесены в архив...». Архивные кампании не восстанавливались. Их присутствие в каталоге и ответы отчётов сохранены. Текущий состав остальных кампаний не доказывает состав на даты базы или запуска. Полнота относительно скрытых/удалённых кампаний, не возвращённых API, не установлена.

Для SEARCH_PROMO/ALL_SKU_PROMO действующий клиент фиксирует ограничение: дневной отчёт даёт расход кампании, а orders=0 не подтверждает отсутствие заказов. В `campaign_daily.csv` рекламные заказы и выручка этих форматов оставлены пустыми с пояснением. Кампания × день сохранена как фактический доступный уровень; по SKU эти кампанийные итоги не распределялись. Их состав к заданным SKU в этой сверке не установлен.

## Сравнение

База 02–29.10, смешанный день 30.10 исключён. Для 31.10 (сб) базовые даты 03,10,17,24.10; для 01.11 (вс) — 04,11,18,25.10; для 02.11 (пн) — 05,12,19,26.10. Ожидаемое значение каждого пост-дня — среднее четырёх соответствующих базовых дней. Ожидание за три дня — сумма этих средних, не сумма двенадцати исходных дней.

В основной сравнительный итог включены только фиксированные SKU × кампания с явно возвращёнными метриками на всех 12 базовых и всех 3 пост-датах. Это 28 из 36 найденных связок, охватывающих все 20 SKU. Другие 8 связок исключены из сравнения; причина и календарь в `sku_pair_coverage.csv`. Метрики за 2 ноября в общей таблице SKU — сумма всех доступных строк, не оценка недостающих связок. В этом снимке расход за 2 ноября совпал с расходом сопоставимой части.

Δ расхода = пост − ожидание; % = 100 × Δ / ожидание только при ненулевой базе. Δ рекламных заказов считается аналогично. Сравнение наблюдательное; оно не доказывает влияние ставок, доступности товара, конкуренции или других факторов. Исходный манифест ставок не получен, откат ставок не подтверждается.

## Покрытие и атрибуция

638/640 SKU × день имеют явные данные. Нет данных по SKU 1510947813 за 29.10 и 4489633315 за 02.10. По всем 20 SKU данные за 2 ноября есть. Однако покрытие SKU × кампания не полное: одна кампания с текущими целевыми связками имеет пустой отчёт, отдельные другие связки присутствуют лишь часть дней. Общий охват кабинета по всем рекламным форматам не заявляется.

Снимок получен 03.11.2026. Рекламные заказы могут поступать с задержкой атрибуции, особенно за 2 ноября; точное окно и финальность конкретного отчёта в этой сверке не подтверждены. Сохранены время получения и исходные ответы для последующей проверки.

В восстановленной БД SELECT 1 успешен, но таблицы истории рекламы отсутствуют. Финальный манифест волны и манифест изменения ставок в проверенных локальных источниках и локальной git-истории не найдены; источники старого Beget не доступны в проекте. Состав семейств и связанные продажи не подтверждены, общие заказы по склейкам не считались. Диагностика старой ограниченной сессии сохранена отдельно в `prior_restricted_session/` и не описывает текущий доступ.
'''
(O/'METHODOLOGY.md').write_text(method)
lines=[f'# Ozon oz_acc1 — разовая сверка 03.11.2026','',f'Сохранено в `{O.resolve()}/`. Снимок и расчёты завершены {now}. Реклама и БД не изменялись.','',
'## Доступ','',f'Режим: {pre["permission_mode"]}. Создание, чтение и удаление тестового файла — успешно. DNS и HTTPS Performance API — успешно, токен получен, список кампаний HTTP 200. Seller API отдельно: POST /v3/product/list (чтение) HTTP 200. Настроенная БД: SELECT 1 = 1, соединение read-only. Реквизиты и токены не выводились и не сохранялись. См. [preflight.json](preflight.json).','',
'## 1. Первая CPC-волна','',
'API вернул 48 кампаний, включая остановленные и архивные. Среди новых CPC-кампаний за 1–3 ноября по МСК после исключения подготовительных ID найден один кандидат:','',
'| ID | Название / статус | Даты | Подтверждение работы | Принадлежность волне |','|---|---|---|---|---|',
'| 49321871 | Бонусы на продвижение товаров-новинок FBS / RUNNING, isAutocreated=true | создана 02.11 в 17:03:53 МСК; fromDate=02.11 | 20 показов за 02.11 | не подтверждена |','',
'Текущий состав кандидата — 1 SKU; состав на момент активации и точное время активации не подтверждены. За 2 ноября: **20 показов, 0 кликов, 0,00 ₽ расхода, CPC не определён, рекламная метрика orders=0**. День неполный: кампания создана внутри дня. Дополнительно за неполный 3 ноября: 17 показов, 0 кликов, 0,00 ₽. Текущий RUNNING сам по себе не используется как доказательство запуска.','',
'Финальный манифест волны не найден. Кампания показана только как кандидат; общий итог по волне, накопленный расход волны и отклонение от 12 500 ₽ **не рассчитывались**. ID 49173478, 49173499, 49173502 исключены: они INACTIVE и содержат суммарно 2953 подготовленные позиции; эти позиции не считаются фактически запущенными.','',
'Источники: [кандидат](wave_candidates.csv), [текущий состав](wave_candidates_composition.csv), [дневная статистика](wave_candidates_daily.csv), [исключённые кампании](wave_excluded_preparation.csv), [все кампании](campaign_inventory.csv). Сырые ответы — `raw/campaigns.json`, `raw/products_49321871.json`, `raw/daily_*.json`.','',
'## 2. Снижение ставок по 20 SKU','',
f'Собрано **{cov["sku_campaign_day_rows"]} строк SKU × кампания × день**, 02.10–02.11 включительно. Явные данные есть для **{cov["observed_sku_days"]}/640** сочетаний SKU × день; все 20 SKU представлены за 2 ноября. Пропуски: 1510947813 за 29.10, 4489633315 за 02.10 — «нет данных». Текущие ставки сохранены для **36 связок в 6 кампаниях**. Пустые отчёты не приравнивались к нулю.','',
f'За **2 ноября**, по всем полученным CPC-строкам заданных SKU: **{count(novview)} показов, {count(novclick)} клика, {fmt(novsp)} ₽, CPC {fmt(novsp/novclick)} ₽, рекламная метрика заказов {count(novord)}**.','',
'| SKU | Показы | Клики | Расход, ₽ | CPC, ₽ | Рекламные заказы API* |','|---|---:|---:|---:|---:|---:|']
for r in nov:lines.append('| '+ ' | '.join([r['sku'],count(r['views']),count(r['clicks']),fmt(r['spend_rub']),fmt(r['cpc_rub']),count(r['ad_orders_api'])])+' |')
lines+=['','*Метрика orders / «Продано товаров», не количество уникальных номеров заказов. Расход — показатель Ozon с НДС; бонусы на уровне SKU отдельно не разделялись. Таблица отражает доступные CPC-строки, не полный охват всех рекламных форматов.','',
f'Для сравнения взяты **28 из 36 связок**, охватывающих все 20 SKU: только связки с явными данными на всех 15 нужных датах. База 02–29.10, 30.10 исключён как смешанный. Ожидание для каждого пост-дня — среднее четырёх таких же дней недели базы. За 31.10–02.11: **{fmt(comp["post_spend_rub"])} ₽ против {fmt(comp["expected_spend_rub"])} ₽; Δ {fmt(comp["delta_spend_rub"])} ₽ ({fmt(comp["spend_delta_percent"])}%)**. Рекламные заказы API: **{count(comp["post_ad_orders_api"])} против {fmt(comp["expected_ad_orders_api"])}; Δ {fmt(comp["delta_ad_orders_api"])}**. Это итог сопоставимой части.','',
'| Пост-день | Базовые даты октября | Расход, ₽ | Ожидание, ₽ | Δ, ₽ | Заказы / ожидание |','|---|---|---:|---:|---:|---:|']
for r,refs in zip(byday,['03, 10, 17, 24','04, 11, 18, 25','05, 12, 19, 26']):lines.append('| '+' | '.join([r['date_msk'],refs,fmt(r['post_spend_rub']),fmt(r['expected_spend_rub']),fmt(r['delta_spend_rub']),count(r['post_ad_orders_api'])+' / '+fmt(r['expected_ad_orders_api'])])+' |')
lines+=['','Крупнейшие отклонения расходов на сопоставимых связках:','','| SKU | Расход поста, ₽ | Ожидание, ₽ | Δ, ₽ | Δ, % | Δ заказов API |','|---|---:|---:|---:|---:|---:|']
for r in largest[:5]:lines.append('| '+' | '.join([r['sku'],fmt(r['post_spend_rub']),fmt(r['expected_spend_rub']),fmt(r['delta_spend_rub']),fmt(r['spend_delta_percent']),fmt(r['delta_ad_orders_api'])])+' |')
lines+=['','Исходный манифест изменения ставок отсутствует: **откат ставок не подтверждён**. Причинная связь по сравнению до/после не утверждается. Заказы за 2 ноября могут иметь задержку атрибуции; это снимок на 3 ноября, точное окно и финальность не подтверждены.','',
'## Источники, ограничения и файлы','',
'- [sku_campaign_daily.csv](sku_campaign_daily.csv) — фактические дневные строки 20 SKU; [current_bids.csv](current_bids.csv) — текущие ставки.',
'- [sku_20261102.csv](sku_20261102.csv) — 2 ноября; [sku_day_coverage.csv](sku_day_coverage.csv) и [sku_pair_coverage.csv](sku_pair_coverage.csv) — покрытие и пропуски.',
'- [comparison_by_sku.csv](comparison_by_sku.csv), [comparison_by_pair.csv](comparison_by_pair.csv), [comparison_by_day.csv](comparison_by_day.csv), [largest_deviations.csv](largest_deviations.csv) — сравнения.',
'- [campaign_daily.csv](campaign_daily.csv) — доступная статистика кампания × день по всем форматам; общие показатели кампаний по SKU не распределялись.',
'- `raw/csv_2026-10-02_2026-10-31.zip`, `raw/csv_2026-11-01_2026-11-03.zip` и одноимённые папки — исходные CSV; `raw/sku_report_*_result.json` — дневные JSON-отчёты по 31 SKU-кампании.',
'- [METHODOLOGY.md](METHODOLOGY.md), [sources.json](sources.json), [manifest_search.json](manifest_search.json), [coverage_summary.json](coverage_summary.json), [audit_summary.json](audit_summary.json), `api_requests.jsonl`, `SHA256SUMS` — методика, источники, аудит и контрольные суммы. CSV и JSON сверены по 1017 строкам и пяти метрикам: расхождений нет.',
'- Недоступны: финальный манифест волны, исходный манифест ставок, история активации через действующие клиенты, исторические рекламные таблицы в восстановленной БД. Состав 9 архивных кампаний API не отдаёт (HTTP 400); архив не менялся. Подтверждённые семейства и связанные продажи не получены, общие заказы по склейкам не считались.',
'- `prior_restricted_session/` — сохранённая история прежних блокировок; текущие проверки доступа успешны.','']
(O/'README.md').write_text('\n'.join(lines))
# Checksums cover every preserved original and derived artifact, including prior blocked history.
files=sorted(p for p in O.rglob('*') if p.is_file() and p.relative_to(O).as_posix()!='SHA256SUMS')
(O/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(O).as_posix()+'\n' for p in files))
print('Report completed:',str((O/'README.md').resolve()))
print('Hashed files:',len(files),'primary Nov 2 spend:',novsp,'CPC:',novsp/novclick)
