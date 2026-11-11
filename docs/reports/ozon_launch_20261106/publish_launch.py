# поток: mkt / ozon
"""Publish campaign creation, confirmed activation, baseline and guard status."""
import collections
import csv
from decimal import Decimal as D
import html
import json
from pathlib import Path
import datetime as dt
from zoneinfo import ZoneInfo

OUT = Path(__file__).parent
KEY = '/api/wb-bids/reports/ozon-launch-2026-11-06'


def read(path):
    with path.open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def fmt(x):
    return f'{D(str(x)):,.2f}'.replace(',', ' ').replace('.', ',')


def esc(x):
    return html.escape(str(x))


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + esc(x) + '</th>' for x in headers) + \
        '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + str(x) + '</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table></div>'


b = json.loads((OUT / 'created_campaign_bindings.json').read_text())
budgets = json.loads((OUT / 'ozon_required_budgets.json').read_text())
budget_by_id = {c['campaign_id']: c for c in budgets['campaigns']}
uploaded = read(OUT / 'uploaded_sku_manifest.csv')
daily = read(OUT / 'baseline/account_daily.csv')
coverage = json.loads((OUT / 'baseline/coverage.json').read_text())
policy = json.loads((OUT / 'risk_policy.json').read_text())
activation = json.loads((OUT / 'activation_status.json').read_text()) if (OUT / 'activation_status.json').exists() else {}
live = json.loads((OUT / 'live_campaign_states.json').read_text()) if (OUT / 'live_campaign_states.json').exists() else {'campaigns': []}
live_states = {str(c['id']): c['state'] for c in live['campaigns']}
events = [json.loads(l) for l in (OUT / 'activation_events.jsonl').read_text().splitlines()] if (OUT / 'activation_events.jsonl').exists() else []
starts = {e['campaign_id']:e['at_utc'] for e in events if e.get('phase') == 'RUNNING_CONFIRMED'}
started = activation.get('state') == 'WAVE1_RUNNING_CONFIRMED'
start_msk = dt.datetime.fromisoformat(activation['first_running_confirmed_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК') if started else 'не установлен'
stop_msk = dt.datetime.fromisoformat(activation['stop_at_msk']).strftime('%d.%m.%Y %H:%M:%S МСК') if started else 'не привязан'
metrics = {'orders': sum(int(r['unique_orders']) for r in daily),
           'ordered_value_rub': sum(D(r['ordered_value_rub']) for r in daily),
           'cpc_spend_rub': sum(D(r['cpc_spend_rub']) for r in daily)}
wave = []
campaign_rows = []; csv_rows = []
cumulative = D(0)
for w in [1, 2, 3]:
    cc = [c for c in b['campaigns'] if c['wave'] == w]
    budget = sum(D(budget_by_id[c['campaign_id']]['required_weekly_budget_rub']) for c in cc)
    cumulative += budget
    wave.append([f'Волна {w}', sum(c['uploaded_skus'] for c in cc), fmt(budget) + ' ₽', fmt(cumulative) + ' ₽'])
for c in b['campaigns']:
    required = D(budget_by_id[c['campaign_id']]['required_weekly_budget_rub'])
    state = live_states.get(c['campaign_id'],c['verified_state'])
    campaign_rows.append([c['wave'], esc(c['title']), c['campaign_id'], c['uploaded_skus'], fmt(required) + ' ₽', 'Работает' if state=='CAMPAIGN_STATE_RUNNING' else 'Неактивна'])
    csv_rows.append({'wave': c['wave'], 'title': c['title'], 'campaign_id': c['campaign_id'],
                    'uploaded_skus': c['uploaded_skus'], 'state': state,
                    'required_weekly_budget_rub': str(required), 'source': 'Ozon dynamic_budget DYNAMIC_BUDGET_REQUIRED',
                    'actual_activation_at': starts.get(c['campaign_id'],''), 'individual_minimum_bid_readback_verified': True})
with (OUT / 'campaign_results.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=list(csv_rows[0])); writer.writeheader(); writer.writerows(csv_rows)
summary = {'created_campaigns': 12, 'uploaded_skus': len(uploaded), 'not_uploaded_own_failures': 169,
    'owner_weekly_reference_rub_wave1': policy.get('owner_weekly_reference_rub'),
    'owner_authorized_wave1_activation': policy.get('owner_authorized_wave1_activation', False),
    'activation_blocker': policy.get('activation_blocker'), 'deletion_allowed': False,
    'sku_risk_action': policy.get('sku_risk_action'),
    'all_campaigns_verified_inactive': not started, 'activation_calls': len(starts),
    'running_campaigns': sum(s=='CAMPAIGN_STATE_RUNNING' for s in live_states.values()),
    'wave1_live_skus': 3451 if started else 0,
    'baseline_from_msk': coverage['from_msk'], 'baseline_through_msk': coverage['through_msk'],
    'baseline_full_days': 28, 'account_baseline': {k: str(v) for k, v in metrics.items()},
    'risk_policy': policy['risk_policy'], 'timer_schedule_msk': '00:00,03:00,06:00,09:00,12:00,15:00,18:00,21:00',
    'monthly_end_stop_bound': policy.get('monthly_deadline_bound',False),
    'actual_start_at': activation.get('first_running_confirmed_at_utc'),
    'all_wave1_running_confirmed_at_utc': activation.get('all_wave1_running_confirmed_at_utc'),
    'scheduled_stop_at_msk': activation.get('stop_at_msk'), 'actual_end_at': None}
(OUT / 'launch_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
first_delivery = ''
if (OUT / 'first_delivery_latest.json').exists():
    delivery = json.loads((OUT / 'first_delivery_latest.json').read_text())
    checked = dt.datetime.fromisoformat(delivery['checked_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    delivery_rows = [[esc(r['group']), r['sku_views'], r['sku_clicks'], fmt(r['sku_spend_rub']), r['sku_ad_orders']]
                     if r['sku_report_rows'] else [esc(r['group']), 'нет строк', '—', '—', '—'] for r in delivery['rows']]
    totals = delivery['totals']
    delivery_rows.append(['Всего по полученным строкам', totals['sku_views'], totals['sku_clicks'], fmt(totals['sku_spend_rub']), totals['sku_ad_orders']])
    first_delivery = '<h2>Статус первой волны</h2><p>Проверено ' + checked + '. Период: ' + esc(delivery.get('period_msk', 'день запуска')) + '. Данные отчёта по SKU:</p>' + table(['Группа', 'Показы', 'Клики', 'Расход, ₽', 'Рекламные заказы по SKU'], delivery_rows)
    first_delivery += '<p>Сводка кампаний отдельно показывает ' + totals['daily_views'] + ' показов, ' + totals['daily_clicks'] + ' кликов, ' + fmt(totals['daily_spend_rub']) + ' ₽ и ' + totals['daily_orders'] + ' заказов. Расхождение источников сохранено; данные предварительные, заказы разных отчётов не складываются. Максимальный расход на SKU ' + fmt(delivery['max_sku_spend_rub']) + ' ₽; автоматических пауз при проверке ' + str(len(delivery['guard_actions'])) + '.</p>'
    if 'sku_model_orders' in totals:
        first_delivery += '<p><strong>Разделение атрибуции Ozon:</strong> поле orders — ' + totals['sku_ad_orders'] + ' покупка рекламируемого SKU на ' + fmt(totals['sku_ad_sales_rub']) + ' ₽; поле modelOrders — ' + totals['sku_model_orders'] + ' покупки по моделям на ' + fmt(totals['sku_model_sales_rub']) + ' ₽. В текущем срезе их сумма ' + str(int(totals['sku_ad_orders']) + int(totals['sku_model_orders'])) + ' совпадает с orders сводки кампаний. Это объясняет разницу числа заказов между уровнями отчёта; прибавлять сводку кампаний к этим полям нельзя.</p>'
    if delivery.get('ad_source_by_day'):
        source_rows = [[day, dt.datetime.fromisoformat(values['sku']['at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M'), dt.datetime.fromisoformat(values['daily']['at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M')] for day, values in delivery['ad_source_by_day'].items()]
        first_delivery += '<p>Итог с запуска объединён по датам, уточнения заменяют прежние строки. Свежесть источников по дням (МСК):</p>' + table(['День', 'Детализация SKU обновлена', 'Итоги кампаний обновлены'], source_rows)
    if delivery.get('cumulative_max_sku_spend_rub'):
        risk_period = delivery['max_sku_spend_period']
        first_delivery += '<p>Максимум расхода SKU за всё время с запуска: ' + fmt(delivery['cumulative_max_sku_spend_rub']) + ' ₽. Максимум в окне защиты ' + risk_period['from_msk'] + ' — ' + risk_period['through_msk'] + ': ' + fmt(delivery['max_sku_spend_rub']) + ' ₽.</p>'
    if delivery.get('days'):
        daily_rows = [[esc(day), d['totals']['sku_views'], d['totals']['sku_clicks'], fmt(d['totals']['sku_spend_rub']), d['totals']['sku_ad_orders']] for day, d in delivery['days'].items()]
        first_delivery += '<p>День запуска и текущий день неполные по времени наблюдения:</p>' + table(['День МСК', 'Показы', 'Клики', 'Расход, ₽', 'Рекламные заказы по SKU'], daily_rows)
    if delivery.get('general_sales'):
        general = delivery['general_sales']
        first_delivery += '<p>По общим заказам после старта: товары первой волны — ' + str(general['own_target_unique_orders']) + '; их склейки Ozon и артикульные семьи — ' + str(general['model_or_article_family_unique_orders']) + ' уникальных заказов, включая заказы самих товаров. Пересечения семей устранены. Это все продажи соответствующих карточек, а не доказанный прирост от рекламы.</p>'
profile_html = ''
intervention_html = ''
intervention_path = OUT / 'external_interventions.jsonl'
if intervention_path.exists():
    intervention_rows = []
    for line in intervention_path.read_text().splitlines():
        event = json.loads(line)
        local_time = dt.datetime.fromisoformat(event['at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК')
        overlap = 'Общая склейка с ' + str(event.get('wave1_article')) + ', SKU ' + str(event.get('wave1_sku')) if event.get('overlaps_wave1') else 'Склейки волны 1 не затрагивает; влияет на общий аккаунт'
        intervention_rows.append([local_time, esc(event['article']), event['campaign_id'], fmt(event['before_bid_rub']) + ' → ' + fmt(event['after_bid_rub']), esc(overlap)])
    intervention_html = '<h2>Другие согласованные изменения рекламы</h2>' + table(['Время', 'Артикул', 'Старая РК', 'Ставка, ₽', 'Связь с экспериментом'], intervention_rows)
    intervention_html += '<p>Ставки самих SKU волны 1 не менялись. Эти события учитываем отдельно при интерпретации последующих продаж склеек и аккаунта. Если время события позже среза выше, результат после него в таблицах ещё не измерен.</p>'
if (OUT / 'family_account_latest.json').exists():
    profile = json.loads((OUT / 'family_account_latest.json').read_text())
    profile_checked = dt.datetime.fromisoformat(profile['checked_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
    profile_start = dt.datetime.fromisoformat(profile['from_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК')
    labels = {'sku': 'Сами 3451 SKU', 'ozon_models': '2926 склеек Ozon', 'children_article_families': '348 семей дочерних', 'account': 'Весь аккаунт'}
    profile_rows = []
    for scope, values in profile['scopes'].items():
        actual = values['actual']; baseline = values['baseline4_mean']
        profile_rows.append([labels[scope], str(actual['orders']) + ' / ' + fmt(baseline['orders']), fmt(actual['ordered_value_rub']) + ' / ' + fmt(baseline['ordered_value_rub']), fmt(values['change_percent']['ordered_value_rub']) + '%'])
    profile_html = '<h2>Склейки, семьи и профиль аккаунта</h2><p>Срез ' + profile_checked + ', от старта ' + profile_start + '. Сравниваем с четырьмя предыдущими окнами, сдвинутыми на 7, 14, 21 и 28 дней: одинаковые дни недели и часы. В таблице текущее значение / среднее четырёх окон.</p>' + table(['Уровень', 'Общие заказы: сейчас / среднее', 'Заказано, ₽: сейчас / среднее', 'Изменение стоимости'], profile_rows)
    account_rows = [[day['day'], day['actual']['orders'], fmt(day['actual']['ordered_value_rub']), fmt(day['baseline4_mean']['ordered_value_rub']), fmt(day['change_percent']['ordered_value_rub']) + '%'] for day in profile['account_days']]
    profile_html += '<p>Весь аккаунт по календарным дням: закрытые дни целиком, сегодняшний до времени среза. Исторические дни сравниваются до того же времени суток.</p>' + table(['День МСК', 'Заказы', 'Заказано, ₽', 'Среднее 4 таких дней, ₽', 'Изменение'], account_rows)
    profile_html += '<p>Склейки Ozon и артикульные семьи дочерних измеряются отдельно и пересекаются. Это все неотменённые заказы соответствующих товаров, включая действующую рекламу соседей и органику. Составы зафиксированы на старте; историческое наличие и состав склеек не восстановлены. Выборка мала, причинный прирост от новой рекламы пока не установлен. Стоимость заказанного не является финансовой выручкой или прибылью.</p>'
    expense_path = OUT / dt.datetime.fromisoformat(profile['checked_at_utc']).astimezone(ZoneInfo('Europe/Moscow')).strftime('monitoring_%Y%m%d_%H%M') / 'account_ad_spend.json'
    if expense_path.exists():
        expenses = json.loads(expense_path.read_text())
        expense_rows = [[day, fmt(values['cpc_sku_rub']), fmt(values['other_formats_rub']), fmt(values['all_reported_ad_spend_rub'])] for day, values in expenses['days'].items()]
        profile_html += '<p>Объём продвижения всего аккаунта по рекламной статистике, по календарным дням; 6 ноября включает часы до старта волны. К CPC добавлены возвращённые daily/json суммы остальных форматов, в этом срезе CPO. Суммы включают бонусное продвижение и не равны списаниям рублями. Для трёх автоматических FBS-кампаний отдельный expense/json подтвердил бонусное финансирование; эти цифры не были разбиты на деньги и бонусы.</p>' + table(['День МСК', 'CPC SKU по отчёту, ₽', 'Остальные форматы, ₽', 'Всего по отчёту, ₽'], expense_rows)
body = f'''
<div class="meta">oz_acc1 · выполнено 06.11.2026 · создание и ставки проверены чтением API</div>
<h1>12 РК созданы: 9073 SKU на индивидуальном минимальном CPC</h1>
<p><b>Свежий срез 11 ноября:</b> <a href="/api/wb-bids/reports/ozon-wave1-profile-2026-11-11">Первая волна обоих аккаунтов и динамика общего профиля: заказы, стоимость и сопоставимые периоды</a>.</p>
<div class="ok"><strong>Первая волна запущена: 4 РК RUNNING, 3451 SKU в составе.</strong>
Подтверждённый старт: <strong>{start_msk}</strong>. Полный запуск четырёх РК подтверждён отдельно.
Свежие индивидуальные минимумы всех 3451 SKU прочитаны и проверены перед активацией;
изменения ставок не понадобились. Остальные 8 РК неактивны.</div>
{first_delivery}
{profile_html}
{intervention_html}
<p><strong>Календарный стоп установлен и включён: {stop_msk}.</strong>
Это общий календарный месяц от первого подтверждённого старта. Следующие волны
планируются через 7 и 14 дней; их активация заранее не выполнялась.</p>
<p>Из зафиксированных 9242 SKU загружены 9073. Индивидуальные минимальные ставки
от 7 до 10 ₽ за клик, медиана 10 ₽. Общая ставка вместо индивидуальных ответов не подставлялась.</p>
<p>169 не загружены по собственным свежим проверкам: у 139 нет доступного остатка,
у 29 нет доступного канала, у одного ошибка карточки. Родственники не исключают доступный SKU;
родители к дочерним не добавлялись и к их волнам не привязаны.</p>
<div class="links"><a href="{KEY}-uploaded">Все 9073: РК и индивидуальные ставки — CSV</a>
<a href="{KEY}-excluded">169 собственных ограничений — CSV</a>
<a href="{KEY}-campaigns">12 созданных РК — CSV</a></div>
<p>На свежем предстартовом срезе доступны 3450 из 3451 SKU первой волны.
Одна позиция недоступна; состав сохранён без удаления, её доступность отмечена
отдельно. Личный информационный сигнал об этом доставлен владельцу.</p>
<p><a href="{KEY}-wave1-live">Первая волна: свежие минимумы, РК, старт и доступность — CSV</a>
· <a href="{KEY}-activation">Фактические события запуска — JSON</a></p>
<h2>Наш ориентир: 12 500 ₽ на неделю первой волны</h2><p><strong>По новой команде установлен внутренний недельный ориентир первой волны 12 500 ₽.</strong> Это мягкий контроль фактического расхода. При превышении — сигнал в личный Telegram Сергея через существующего бота и оценка показателей в BI. Автоматической остановки только за превышение ориентира нет. Недели считаются отдельными семидневными календарными периодами МСК от даты первого запуска, отдельно от недельного цикла Ozon.</p><p>Сигнал — при первом превышении, затем при дополнительных 25% ориентира; каждую проверку оценка обновляется. В оценке расходы, заказы SKU и модели, крупнейшие расходы и общий профиль аккаунта. Данные о заказах в оперативной оценке относятся к текущему и предыдущему дням и подписаны этим периодом.</p><h2>Технический недельный минимум Ozon</h2>
<p>Суммы получены для каждой созданной РК методом <code>/external/api/dynamic_budget</code>,
тип <code>DYNAMIC_BUDGET_REQUIRED</code>. Ozon вернул 2000 ₽ недельного бюджета на
загруженную позицию. Это <strong>минимум недельного бюджета в настройках</strong>,
а не прогноз фактических списаний или сумма обязательного пополнения баланса.
Активация четырёх РК первой волны успешно выполнена; минимум настроек
остается отдельным от внутреннего ориентира фактического расхода.</p>
{table(['Волна', 'Загружено SKU', 'Недельный минимум РК этой волны', 'Если работают все волны к этому моменту'], wave)}
<p>При продолжении ранних волн после добавления поздних недельный минимум настроек
достигает 18 146 000 ₽. Фактический расход зависит от кликов и доступного трафика;
внутренний ориентир первой волны — 12 500 ₽/неделю с сигналом и оценкой превышения. Первая волна запущена на индивидуальных минимумах. Все товары сохранены в составе; автопауза по риску допускается только от 1000 ₽ без подтвержденных заказов SKU/семьи.</p>
{table(['Волна', 'Название РК', 'ID Ozon', 'SKU', 'Минимум в неделю', 'Состояние'], campaign_rows)}
<p><a href="{KEY}-budgets">Подтверждённые расчёты Ozon — JSON</a></p>
<h2>Показатели до запуска</h2>
<p>Заморожены <strong>28 полных дней: 09.10–05.11.2026 МСК</strong>. Общие FBS/FBO
перечитаны заново, задержанные заказы и отмены учтены по состоянию на сбор.
Предыдущая подробная рекламная история переиспользована, 5 ноября добавлено оперативным методом.</p>
{table(['Весь аккаунт за 28 дней', 'Значение'], [['Уникальные заказы без отмен', metrics['orders']],
 ['Стоимость заказанного', fmt(metrics['ordered_value_rub']) + ' ₽'], ['CPC-расход всех кампаний', fmt(metrics['cpc_spend_rub']) + ' ₽']])}
<p>Стоимость заказанного — не финансовая выручка или прибыль. CPO-расход пока
не измерен этим источником и не записан нулём. Есть расхождение 100 ₽ за 5 ноября
между расходом SKU и кампании одной прежней РК; сохранено явно. Для аккаунта взят
кампанийный daily/json, целевых SKU это расхождение не затрагивает.</p>
<div class="links"><a href="{KEY}-baseline-skus">База каждого из 9242 SKU — CSV</a>
<a href="{KEY}-baseline-families">SKU / склейки / семьи: общие продажи по дням — CSV</a>
<a href="{KEY}-baseline-family-totals">Итоги склеек и семей за 28 дней — CSV</a>
<a href="{KEY}-baseline-account">Весь аккаунт по дням — CSV</a></div>
<h2>Как будем оценивать</h2>
<ol><li><strong>Каждый SKU:</strong> рекламные показы, клики, расход, CPC, рекламные
заказы/продажи и ДРР; отдельно все общие заказы и стоимость заказанного.</li>
<li><strong>Вся склейка Ozon:</strong> все уникальные заказы и стоимость всей модели,
включая соседние карточки; собственные и соседние продажи отдельно. Для дочерних
дополнительно артикульная семья «родитель + дочерние». Заказ в объединении считается один раз.</li>
<li><strong>Весь аккаунт — приоритет:</strong> общий CPC-расход, новые РК отдельно,
все заказы, стоимость заказанного, отмены, цены/наличие и параллельные изменения.
CPO показывается отдельно при наличии достоверного источника.</li></ol>
<p>Три часа — частота защиты и оперативного наблюдения. Эффект оцениваем недельными
срезами одинаковых дней недели и длительностей, затем по полному сроку. Редкие SKU
с недостаточными данными остаются неопределёнными, отсутствие значимости не равно нулевому эффекту.</p>
<p>В итоговом анализе сохраняются все исходные 9242 SKU, включая недопущенные
и остановленные сторожем. Причины отсутствия воздействия, даты стопов и фактическое
время работы учитываются отдельно. Общие заказы после стопа продолжают попадать
в сохраняемое сырьё аккаунта.</p>
<p>Бандлы X2/X6/X10 входят последовательно в первую/вторую/третью волну. Связанные
склейки могут получать накопленное воздействие нескольких волн; это не независимые
T/C. Ноябрьский контроль не назначен, исторические E5/E8 сохранены.</p>
<p>Первый подтверждённый старт — {start_msk}; конец общего месяца — {stop_msk}. День запуска неполный: дневные сравнения после старта ведём по полным дням, отдельно от оперативных проверок. Для поздних волн сохраняем общую базу до D0 и отдельный срез до их включения с отметкой уже действующих волн.</p>
<p>С фиксированными 20 SKU, ставки которых снижены 30 октября, прямого пересечения нет.
В пяти их склейках теперь есть по одному SKU новой волны. После этого запуска
изменения этих склеек и всего аккаунта нельзя приписывать только снижению ставок.
Ставки прежних 20 SKU не менялись.</p>
<h2>Защита каждые три часа</h2>
<p><strong>Таймер установлен и включён.</strong> Расписание МСК:
00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00. Первый живой прогон
успешен и после запуска: 4 РК работают, 8 неактивны, профиль аккаунта снят. Автоматических пауз не потребовалось.</p>
<ul><li><strong>Автопауза только от 1000 ₽ на один SKU при подтверждённом отсутствии заказов.</strong>
Нет ни рекламных заказов SKU/модели, ни общих заказов SKU/склейки/артикульной семьи.
Используем текущий и предыдущий календарные дни; общие заказы до фактического включения
соответствующей РК не считаются результатом после запуска.</li>
<li>Индивидуальная пауза CPC SKU без удаления в API не подтверждена.
При указанном срабатывании приостанавливается соответствующая РК целиком;
все товары и ставки сохраняются для дальнейшего включения.</li>
<li><strong>Остальные случаи — только сигналы:</strong> 300 ₽ без заказов,
500 ₽ при недоступных общих заказах, расхождения данных, потеря телеметрии,
превышение ориентира 12 500 ₽/неделю. Они сами не вызывают автопаузу.</li>
<li>Сигналы доставляются только владельцу @sergkidman через @pro_dropbox_bot.
Автовключения, повышения ставок, удаления SKU и изменений прежних РК нет.</li></ul>
<p>Неизвестные заказы не считаются нулями: без полного подтверждения
отсутствия общих заказов автоматическая пауза по 1000 ₽ не применяется.
Каждое решение и доставка сигнала журналируются.</p>
<p>Остановка выполняется на ближайшей трёхчасовой проверке. Данные Ozon могут
запаздывать; этот контроль не является гарантированным денежным cap.</p>
<p><strong>Месячный стоп установлен: {stop_msk}.</strong> Отдельный stop-only таймер
подписан на эти 12 новых РК и не затрагивает прежние кампании.</p>
<div class="notice" id="guard-live">Загрузка последнего состояния трёхчасовой проверки…</div>
<p><a href="{KEY}-guard">Последнее состояние проверки — JSON</a> ·
<a href="{KEY}-policy">Точные правила защиты — JSON</a></p>
<h2>Следующий шаг</h2>
<p>Следующая проверка — по трёхчасовому расписанию. Наблюдаем первые показы, расход и заказы SKU, семей и аккаунта. Даты следующих волн по плану — 13 и 20 ноября; до их включения снимаем отдельный срез и оцениваем первую волну. Общий календарный стоп — {stop_msk}. Не увеличиваем ставки автоматически и не делаем вывод о пользе рекламы по первым часам.</p>
'''
css = '''*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#182230;font:17px/1.65 system-ui,sans-serif}main{max-width:1250px;margin:28px auto;padding:32px;background:white;border:1px solid #e4e7ec;border-radius:14px}h1{font-size:29px;line-height:1.3}h2{font-size:23px;margin-top:32px}a{color:#175cd3}p,li{overflow-wrap:anywhere}li{margin:9px 0}.meta{color:#667085;font-size:14px}.notice,.ok{padding:17px;border-radius:10px;margin:22px 0}.notice{border:1px solid #fedf89;background:#fffaeb}.ok{border:1px solid #abefc6;background:#ecfdf3}.links{display:flex;gap:12px;flex-wrap:wrap}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;margin:16px 0}th,td{padding:11px;border-bottom:1px solid #e4e7ec;text-align:left}th{background:#f8fafc}@media(max-width:640px){main{margin:0;padding:20px 16px;border:0;border-radius:0}h1{font-size:25px}th,td{padding:8px}}'''
script = '''<script>fetch('/api/wb-bids/reports/ozon-launch-2026-11-06-guard',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}).then(s=>{const labels={WAITING_FOR_ACTIVATION_ALL_BOUND_CAMPAIGNS_INACTIVE:'Все РК неактивны, ожидают запуска',CHECKED:'Проверка выполнена',TELEMETRY_ERROR:'Ошибка получения данных'};const active=(s.campaigns||[]).filter(c=>c.state==='CAMPAIGN_STATE_RUNNING').length;document.getElementById('guard-live').textContent='Проверено: '+new Date(s.checked_at_utc).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК · '+(labels[s.status]||'Проверка состояния')+' · работающих РК: '+active+' из 12 · действий остановки: '+(s.actions||[]).length}).catch(()=>{document.getElementById('guard-live').textContent='Последний статус временно недоступен; доступна прямая ссылка JSON ниже.'})</script>'''
(OUT / 'index.html').write_text('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<meta name="robots" content="noindex,nofollow,noarchive"><title>Ozon: РК созданы, бюджеты и защита</title>'
    '<style>' + css + '</style></head><body><main>' + body + '</main>' + script + '</body></html>')
print('Launch report rendered: first wave RUNNING, other waves inactive; confirmed start/stop and 1000 RUB-only pause policy.')
