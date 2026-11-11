# поток: mkt
"""Offline report of a blocked one-off verification; never calls marketplace APIs."""
import csv,datetime as dt,hashlib,json,pathlib,subprocess
O=pathlib.Path('/root/ozon_recovery_20261103'); P=pathlib.Path('/opt/mp-analytics')
SKUS='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split()
now=dt.datetime.now(dt.timezone.utc).isoformat()
def save(name,obj): (O/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n')
def csvout(name,fields,rows):
 with (O/name).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
start,end=dt.date(2026,10,2),dt.date(2026,11,2)
days=[start+dt.timedelta(days=i) for i in range((end-start).days+1)]
base=[d for d in days if d<=dt.date(2026,10,29)]
post=[dt.date(2026,10,31),dt.date(2026,11,1),dt.date(2026,11,2)]
matched=[{'post_date':d.isoformat(),'weekday':d.strftime('%A'),'baseline_dates':[b.isoformat() for b in base if b.weekday()==d.weekday()]} for d in post]
save('comparison_design.json',{'timezone':'Europe/Moscow','baseline':['2026-10-02','2026-10-29'],'mixed_day_excluded':'2026-10-30','post':['2026-10-31','2026-11-02'],'same_weekdays':matched,'method':'Для каждого SKU × кампания: среднее четырёх базовых дат того же дня недели; сумма трёх средних — ожидаемый расход за пост-период. Δ=факт−ожидание; %=100×Δ/ожидание только при ненулевой базе и достаточном покрытии. Аналогично заказы. Только наблюдение, без причинного вывода.','comparison_computed':False,'reason':'нет данных'})
reasons=['DNS resolution of api-performance.ozon.ru failed before an API response','Local PostgreSQL connection unavailable in this session (Operation not permitted)','Requested destination is read-only in this session']
common={'account':'oz_acc1','status':'нет данных','collected_at_utc':now,'records':None,'blockers':reasons}
save('wave1_check.json',{**common,'requested_window_msk':['2026-11-01','2026-11-03'],'excluded_preparation_ids':['49173478','49173499','49173502'],'candidate_campaigns':None,'campaign_membership_confirmed':False,'operation_confirmed':False,'activation_history':None,'composition_at_launch':None,'current_composition':None,'november_2_statistics':None,'partial_day':None,'actual_launched_skus':None,'prepared_2953_skus_used_as_launch_count':False,'wave_total_computed':False,'cumulative_spend_rub':None,'reference_rub':12500,'deviation_from_reference_rub':None,'manifest':'не найден в проверенных локальных источниках; удалённые источники недоступны'})
save('sku20_statistics_status.json',{**common,'requested_granularity':'SKU × campaign × day','actual_available_granularity':None,'date_from':'2026-10-02','date_to':'2026-11-02','skus':SKUS,'confirmed_sku_coverage':0,'requested_sku_coverage':20,'confirmed_days':0,'requested_days':32,'november_2_metrics':None,'spend_delta_rub':None,'spend_delta_percent':None,'order_delta':None,'largest_deviations':None,'campaign_metrics_allocated_to_sku':False,'bid_rollback_confirmed':False,'initial_bid_manifest':'не найден в проверенных локальных источниках'})
save('current_bids_status.json',{**common,'requested_granularity':'SKU × campaign','skus':SKUS,'bid_unit_in_existing_client':'micro-rubles; divide by 1000000 only for observed bids','historical_bid_inferred_from_current_bid':False})
csvout('sku_day_coverage.csv',['sku','date_msk','campaign_id','status','reason'],({'sku':sku,'date_msk':str(day),'campaign_id':'','status':'нет данных','reason':'Performance API недоступен; связки с кампаниями не установлены'} for sku in SKUS for day in days))
local_sources=[]
for name in ['AGENTS.md','CLAUDE.md','.claude/rules/kb-index.md','.claude/rules/ozon.md','.claude/rules/data-layer.md','docs/BRIEF_MKT.md','docs/BRIEF_MKT_OZON.md','collectors/ozon_ads.py','collectors/ozon_bids.py','collectors/ozon.py','tools/ozon_bid_ramp.py']:
 p=P/name;local_sources.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
git=subprocess.run(['git','-C',str(P),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
save('source_audit.json',{'hostname':'cloud-001','project':str(P),'git_head':git,'collected_at_utc':now,'local_sources':local_sources,'searched_local_scopes':['docs','tools','collectors','.claude','data','backups','logs','local git history'],'wave_manifest_search':'нет подтверждённого финального манифеста ноябрьской CPC-волны в проверенных источниках','bid_manifest_search':'нет подтверждённого манифеста снижения ставок 30 октября по заданным 20 SKU','unavailable_sources':['Performance API campaigns, including stopped and archived if returned','Performance API campaign products and bids','Performance API activation history (endpoint not established)','Performance API campaign daily statistics','Performance API asynchronous SKU statistics reports','PostgreSQL historical bids/statistics/journals','Beget collectors and their original manifests','confirmed family composition and associated sales'],'note':'Local historical cohorts and old reports are not evidence for the requested dates or composition.'})
save('request_audit.json',{'latest_access_recheck':json.loads((O/'access_recheck.json').read_text()),'account':'oz_acc1','project':str(P),'attempts':[{'operation':'POST /api/client/token through collectors.ozon_ads._token','result':'DNS failure before API response; token not received','credentials_were_missing':True,'phase':'initial check'},{'operation':'PostgreSQL read-only session through core.db','result':'local socket unavailable: Operation not permitted'},{'operation':'mkdir docs/reports/ozon_recovery_20261103','result':'Read-only file system'}],'statistics_requests_sent':0,'campaign_mutations':0,'bids_mutations':0,'budget_mutations':0,'schedule_mutations':0,'database_writes':0,'token_or_secret_values_saved':False,'requested_output_path':str(P/'docs/reports/ozon_recovery_20261103'),'actual_output_path':str(O),'placement_complete':False})
report=f'''# Разовая сверка Ozon oz_acc1 — 03.11.2026

Состояние: обе рекламные проверки заблокированы, фактическая статистика не получена. «Нет данных» не означает нулевую активность.
Проверено на cloud-001, проект `/opt/mp-analytics`, время фиксации `{now}`. Использованы существующие модули проекта; старый коллектор не восстанавливался, его результаты не подменялись.

## Доступ и сохранение

- Seller API: реквизиты oz_acc1 настроены; успешный сетевой запрос в этой проверке не подтверждён.
- Performance API использует отдельные реквизиты `OZON_PERF_CLIENT_ID_ACC1` и `OZON_PERF_SECRET_ACC1`. При первичной проверке реквизиты отсутствовали. После внесения пользователем повторная проверка подтвердила наличие обоих реквизитов в `.env` и их загрузку существующим клиентом. Авторизация ещё не подтверждена: запрос с настроенными реквизитами также остановился на DNS. Подробности и время — в `access_recheck.json`. Значения секретов не сохранялись и не выводились.
- Попытка получить токен через `collectors.ozon_ads._token('oz_acc1')` остановилась на разрешении DNS `api-performance.ozon.ru`. HTTP-ответ Ozon не получен, права/действительность реквизитов не проверены. Это не ответ 401/403 от Ozon.
- Локальная БД недоступна: подключение через существующий `core.db` завершилось `Operation not permitted` для сокета PostgreSQL. Содержимое исторических таблиц и их покрытие не установлены.
- Запрошенная папка `/opt/mp-analytics/docs/reports/ozon_recovery_20261103/` недоступна для записи в этой сессии (`Read-only file system`). Фактическое место сохранения: `/root/ozon_recovery_20261103/`. Размещение по запрошенному пути не выполнено.

## 1. Первая CPC-волна 2–8 ноября

За 1–3 ноября по МСК список кампаний, включая остановленные и архивные, получить не удалось. ID кандидатов, названия, статусы, даты запуска и состав: **нет данных**. История активации и факт работы по статистике: **нет данных**. Создание, расписание, текущий статус и фактический запуск не приравниваются друг к другу.

Подготовительные ID `49173478`, `49173499`, `49173502` исключены из планируемого отбора. Финальный манифест ноябрьской волны в проверенных локальных документах/файлах не найден; удалённые источники и Beget недоступны в этой проверке. Принадлежность какой-либо кампании к волне не подтверждена. Отсутствие найденных кандидатов не доказывает отсутствие новых кампаний.

За 2 ноября показы, клики, расход, CPC и рекламные заказы: **нет данных**. Неполный день запуска установить невозможно. 2953 подготовленных SKU не считаются запущенными. Общий итог по волне, накопленный фактический расход и отклонение от 12 500 ₽ не рассчитывались: состав и покрытие не подтверждены. См. `wave1_check.json`.

## 2. Снижение ставок по 20 SKU

Все 20 заданных SKU перечислены в `sku20_statistics_status.json`. За 02.10–02.11 включительно запрошены 32 календарных дня. Фактически подтверждённое статистикой покрытие: **0/32 дней, 0/20 SKU**; это отсутствие полученных данных, не нулевые показатели. В `sku_day_coverage.csv` сохранены 640 отметок SKU × день «нет данных»; это карта пробелов, не выгрузка статистики. Связки SKU × кампания не установлены.

Фактический доступный уровень детализации API не установлен из-за блокировки до получения ответов. В проекте существуют синхронный отчёт кампания × день (`collectors/ozon_ads.py`) и клиент асинхронных отчётов по SKU (`tools/ozon_bid_ramp.py`); их наличие не подтверждает, что нужные отчёты доступны сейчас. Показатели кампаний по SKU не распределялись. Текущие ставки по SKU × кампания: **нет данных** (`current_bids_status.json`).

Показы, клики, расход, CPC и рекламные заказы за 2 ноября, разница расходов в рублях/процентах, изменение заказов и крупнейшие отклонения: **нет данных**. Исходный манифест снижения ставок по этим 20 SKU локально не найден. Откат ставок не утверждается.

Дизайн сравнения сохранён в `comparison_design.json`: база 02–29.10; смешанный день 30.10 исключён; пост-период 31.10–02.11. Сопоставления по дням недели:

| Пост-дата | Базовые даты октября |
|---|---|
| 31.10, суббота | 03, 10, 17, 24 |
| 01.11, воскресенье | 04, 11, 18, 25 |
| 02.11, понедельник | 05, 12, 19, 26 |

Ожидаемый расход трёх пост-дней — сумма средних четырёх базовых дат соответствующего дня недели при сопоставимом и достаточном покрытии. Δ = факт − ожидаемое; % = 100 × Δ / ожидаемое при ненулевой базе. CPC = расход / клики при положительных кликах; пустой ответ — «нет данных». Расчёты не выполнены из-за отсутствия наблюдений. Причинных выводов по сравнению до/после нет.

## Общие ограничения

Рекламные заказы могут уточняться с задержкой атрибуции, особенно за 2 ноября при проверке 3 ноября; точное окно задержки и финальность данных в этой сессии не подтверждены. Текущий состав и текущая ставка не доказывают исторический состав и ставки. Общие заказы по склейкам не считались: подтверждённый состав семейств и доступные продажи не получены.

Кампании, ставки, бюджеты, состав, статусы и расписания не менялись. БД не изменялась. Токен не получен; запросы статистики не отправлены вследствие недоступности авторизации. Секреты не раскрывались.

## Подтверждённые ограничения текущего исполнения

Повторная проверка после сообщения пользователя о предоставленных доступах подтвердила: реквизиты Performance API загружаются, но файловая система целевого проекта смонтирована `ro`; TCP-подключение к PostgreSQL 127.0.0.1:5433 запрещено (`PermissionError`, errno 1); DNS возвращает EAI_AGAIN (-3). Это ограничения данной среды исполнения, а не подтверждение неполадок Ozon или хоста вне этой среды. Новые ключи не требуются. Подробности — `execution_access_diagnostic.json`.

## Что нужно для завершения

Реквизиты Performance API для oz_acc1 уже настроены. Остаются: сетевой доступ и DNS к `api-performance.ozon.ru`; доступ к историческим данным/манифестам, если требуется подтверждение принадлежности и изменения ставок; доступ на запись именно к целевой папке отчёта. После получения доступа следует выполнить обе выгрузки через существующие клиенты и заменить статус «нет данных» только реальными ответами, сохраняя происхождение и время каждого ответа.

## Сохранённые файлы

Все пути ниже относятся к `/root/ozon_recovery_20261103/`:

- `README.md` — этот отчёт.
- `execution_access_diagnostic.json` — подтверждённые ограничения среды исполнения.
- `access_recheck.json`, `recheck.py` — актуальный результат повторной проверки и её код.
- `performance_access_check.json`, `seller_connection_check.json`, `credential_presence_check.json` — результат первичной проверки до внесения новых реквизитов, без значений секретов.
- `api_probe_error.json`, `db_probe_error.json` — исходные диагностические ошибки без секретов.
- `wave1_check.json` — статус проверки волны; неизвестные показатели имеют null.
- `sku20_statistics_status.json`, `current_bids_status.json` — статусы статистики и ставок.
- `sku_day_coverage.csv` — карта неполученных данных по 20 SKU и 32 дням.
- `comparison_design.json` — точные даты и метод сопоставления.
- `source_audit.json`, `request_audit.json` — источники, ограничения, действия и фактические пути.
- `probe.py`, `build_report.py` — исполненная проверка и воспроизводимое формирование отчёта; не штатный коллектор.
- `SHA256SUMS` — контрольные суммы сохранённых файлов. Папка `raw/` пуста: ответы со статистикой не получены.
'''
(O/'README.md').write_text(report)
assert len(SKUS)==20 and len(set(SKUS))==20 and len(days)==32
assert all(len(x['baseline_dates'])==4 for x in matched)
with (O/'sku_day_coverage.csv').open() as f: assert len(list(csv.DictReader(f)))==640
for p in O.glob('*.json'): json.loads(p.read_text())
files=sorted(p for p in O.iterdir() if p.is_file() and p.name!='SHA256SUMS')
(O/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files))
print('Saved',len(files)+1,'files;',len(SKUS),'SKU;',len(days),'days; 640 missing-data markers; no statistics fabricated.')
