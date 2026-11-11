# поток: mkt / ozon
"""Render verified acc2 W1 launch and retained return list from existing operation evidence."""
import csv
import datetime as dt
from decimal import Decimal as D
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

A = Path(__file__).parent
P = A.parent
load = lambda path: json.loads(path.read_text())
save = lambda path, obj: path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
readcsv = lambda path: list(csv.DictReader(path.open(encoding='utf-8-sig')))
status = load(P / 'activation_status.json')
assert status['state'] == 'WAVE1_RUNNING_CONFIRMED' and status['wave1_uploaded_skus'] == 3851
cfg = load(P / 'risk_policy.json')
binding = load(Path(cfg['bindings_path']))
proof = load(A / 'live_safety_verification.json')
run = Path(status['floor_refresh_run'])
guard = load(P / 'guard_status.json')
events = [json.loads(line) for line in (P / 'activation_events.jsonl').read_text().splitlines()]
starts = {x['campaign_id']: x['at_utc'] for x in events if x['phase'] == 'RUNNING_CONFIRMED'}
assert len(starts) == 4 and cfg['monthly_deadline_bound'] and cfg['monthly_stop_status'] == 'BOUND_INSTALLED_FIRST_RUNNING_CONFIRMED'
fresh_floor = load(run / 'individual_minima_micro_rub.json')
ready = readcsv(A / 'READY_3851_NOT_APPLIED.csv')
deferred = readcsv(A / 'DEFERRED_19_NOT_APPLIED.csv')
assert len(ready) == len(fresh_floor) == 3851 and len(deferred) == 19
campaigns = {str(x['id']): x for x in status['campaigns']}
live_states = {x['campaign_id']: x['state'] for x in guard['campaigns']}
for c in binding['campaigns']:
    rb = load(Path(cfg['bindings_path']).parent / c['readback_file'])
    desired = {x['sku'] for x in ready if x['actual_campaign_id'] == c['campaign_id']}
    assert {str(x['sku']) for x in rb['products']} == desired
    assert all(D(str(x['bid'])) == D(fresh_floor[str(x['sku'])]) for x in rb['products'])
launched = [{**r, 'actual_activation_at': starts[r['actual_campaign_id']], 'actual_campaign_state': live_states[r['actual_campaign_id']],
             'activation_called': True, 'bid_rub': str(D(fresh_floor[r['sku']]) / 1000000),
             'bid_micro_rub': fresh_floor[r['sku']], 'SKU_present_after_prelaunch_exclusions': True} for r in ready]
removed_at = {}
for line in (A / 'exclusions_applied/operations.jsonl').read_text().splitlines():
    event = json.loads(line)
    if event['phase'] == 'RESPONSE' and event['http'] == 200:
        removed_at[event['path'].split('/')[4]] = event['at_utc']
return_rows = []
for r in deferred:
    cid = r['actual_campaign_id']
    before = load(A / f'exclusions_applied/before_{cid}.json')
    old = next(x for x in before['products'] if str(x['sku']) == r['sku'])
    return_rows.append({'account': 'oz_acc2', 'sku': r['sku'], 'offer_id': r['offer_id'],
                        'from_campaign_id': cid, 'from_campaign_title': r['campaign_title'],
                        'reason_at_prelaunch': r['fresh_failures'], 'last_available_stock_units': r['fresh_available_stock_units'],
                        'last_bid_before_exclusion_micro_rub': str(old['bid']), 'excluded_at_utc': removed_at[cid],
                        'automatic_return_allowed': False, 'return_requires_fresh_eligibility_and_individual_floor': True})


def output(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


output(A / 'launched_wave1.csv', launched)
output(A / 'deferred_19_for_return.csv', return_rows)
base = readcsv(A / 'baseline/sku_model_family_baseline.csv')
launched_skus = {r['sku'] for r in launched}
output(A / 'baseline/launched_sku_model_family_baseline.csv', [r for r in base if r['sku'] in launched_skus])
baseline = load(A / 'baseline/coverage.json')
precut = load(run / 'account_immediately_before_activation.json')
summary = {'account': 'oz_acc2', 'state': status['state'], 'published_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
           'first_running_confirmed_at_utc': status['first_running_confirmed_at_utc'],
           'all_wave1_running_confirmed_at_utc': status['all_wave1_running_confirmed_at_utc'],
           'stop_at_msk': status['stop_at_msk'], 'wave1_uploaded_skus': 3851, 'deferred_skus': 19,
           'campaigns': [{'campaign_id': c['campaign_id'], 'title': c['title'], 'SKU_count': c['uploaded_skus'],
                          'state': live_states[c['campaign_id']], 'first_running_confirmed_at_utc': starts[c['campaign_id']]}
                         for c in binding['campaigns']],
           'individual_minima_verified': 3851, 'minima_changed_before_launch': status['changed_bids'],
           'owner_weekly_reference_rub': 12500, 'weekly_reference_is_hard_cap': False,
           'automatic_zero_order_pause_rub_per_SKU': 1000, 'guard_installed': True,
           'month_stop_installed': True, 'later_wave_activation_scheduled': False,
           'proposed_wave2_at_msk': status['proposed_wave2_at_msk'], 'baseline': baseline,
           'preactivation_today_snapshot': precut['scopes'], 'first_active_guard_status': proof['first_active_guard_status'],
           'guard_checked_at_utc': guard['checked_at_utc'], 'guard_current_window_spend_rub': guard.get('new_campaign_CPC_spend_rub'),
           'data_latency_not_proof_no_delivery': True, 'ordered_value_is_financial_revenue': False}
save(P / 'live_summary.json', summary)
save(P / 'execution_status.json', {**summary, 'actual_activation_at': status['first_running_confirmed_at_utc'],
     'actual_stop_at': status['stop_at_msk'], 'owner_authorized_wave1_activation': True, 'activation_blocker': None,
     'account2_guard_installed': True, 'account2_month_stop_installed': True, 'all_inactive_verified': False,
     'owner_actual_weekly_spend_authorized_rub': 12500})
limits_path = P / 'limits_from_acc1/limits_public.json'
limits = load(limits_path)
limits.update(actual_activation_at=status['first_running_confirmed_at_utc'], actual_stop_at=status['stop_at_msk'],
              guard_installed=True, monthly_stop_installed=True)
save(limits_path, limits)
labels = {'wave1_children': 'Дочерние', 'wave1_new_6months': 'Новинки за полгода', 'wave1_old': 'Старые', 'wave1_bundles_x3': 'Бандлы X3'}
entries = {c['plan_key']: c for c in binding['campaigns']}
tz = ZoneInfo('Europe/Moscow')
start = dt.datetime.fromisoformat(status['first_running_confirmed_at_utc']).astimezone(tz).strftime('%d.%m.%Y %H:%M:%S МСК')
stop = dt.datetime.fromisoformat(status['stop_at_msk']).strftime('%d.%m.%Y %H:%M:%S МСК')


def table(head, rows):
    return '<table><tr>' + ''.join('<th>' + html.escape(str(x)) + '</th>' for x in head) + '</tr>' + ''.join(
        '<tr>' + ''.join('<td>' + html.escape(str(x)) + '</td>' for x in row) + '</tr>' for row in rows) + '</table>'


body = '<h1>Дисквэр · первая волна запущена</h1>'
body += '<p><b>Свежий срез 11 ноября:</b> <a href="/api/wb-bids/reports/ozon-wave1-profile-2026-11-11">Первая волна обоих аккаунтов и динамика общего профиля: заказы, стоимость и сопоставимые периоды</a>.</p>'
body += f'<p><b>Фактический старт: {start}. Четыре РК, 3 851 SKU.</b> Статус RUNNING подтверждён API для каждой кампании. Согласованные 19 SKU исключены только из новых РК, точный список и ставки до исключения сохранены для возврата. Бонусная РК и карточки товаров не менялись.</p>'
body += table(['Группа', 'ID РК', 'SKU', 'Минимальные ставки, ₽', 'Статус при запуске'], [
    [labels[k], entries[k]['campaign_id'], entries[k]['uploaded_skus'],
     ' / '.join(sorted({x['bid_rub'] for x in launched if x['campaign_key'] == k}, key=D)), 'RUNNING'] for k in labels])
body += '<p>3 851 ставка проверена чтением назад по свежим индивидуальным минимумам. Перед стартом у 12 SKU ставка приведена к изменившемуся минимуму Ozon. Ставки остальных SKU сохранены по подтверждённому минимуму. Технические недельные бюджеты РК не являются фактическим расходом.</p>'
body += f'<h2>Контроль и завершение</h2><p><b>Месячная остановка: {stop}.</b> Отдельный таймер акк2 установлен, enabled+active; дата сверена с первым подтверждённым RUNNING. Реальный сервис остановки проверен до срока: завершился успешно без остановки рекламы. Таймер акк1 не менялся.</p>'
body += '<p>Проверка каждые 3 часа: 00/03/06/09/12/15/18/21 МСК. Общий ориентир W1 — 12 500 ₽/неделю; превышение вызывает сигнал и оценку, это не жёсткий cap. Автопауза затронутой новой РК — от 1 000 ₽ на SKU при подтверждённом отсутствии собственных и связанных заказов. Все оставшиеся SKU сохраняются; автоматического включения нет. Сигналы от 300 ₽ без заказов, от 500 ₽ при неизвестных заказах. Неизвестное не считается нулём; потеря данных — только сигнал с повтором не чаще 6 часов. Сигналы доступны в BI и локальном журнале.</p>'
body += '<div id="guard-live">Первая проверка после активации: CHECKED, автоматических пауз 0. Показатели Ozon могут поступать с задержкой; пустая первая телеметрия не доказывает отсутствие показов.</div>'
body += f'<h2>База до запуска</h2><p>28 полных дней 12.10–08.11: {baseline["unique_account_orders"]} уникальных неотменённых заказов аккаунта, {baseline["account_ordered_value_rub"]} ₽ заказанной стоимости. Обновлены только последние 3 дня 06–08.11, старые дни взяты из сохранённых raw. Неизвестных проданных моделей 0. Дополнительно непосредственно перед активацией сняты заказы текущего дня: {precut["scopes"]["account"]["unique_orders"]} / {precut["scopes"]["account"]["ordered_value_rub"]} ₽. Заказанная стоимость не является финансовой реализацией или прибылью.</p>'
body += '<p>Измеряем отдельно каждый SKU, фактическую Ozon-модель и профиль всего аккаунта; артикульная семья — дополнительная связь, не доказательство склейки. Родитель не нужен для допуска дочернего. Существующая пара дочерних 09278T3Z11SY / 09279QTTL6OR с общим model_id сохранена; реклама её не создавала. Глобальное CPO продолжает действовать фоном, восстановление из архива — отдельное воздействие.</p>'
body += '<h2>Оставшиеся этапы</h2><p>Вторая волна предполагается через 7 дней после фактического старта, 16 ноября в 19:18 МСК; автоматически не запланирована и не активирована. В неё по исходному плану идут следующие кратности бандлов, оставшиеся старые товары и накопленные восстановленные дочерние после индивидуальной проверки. Общий эксперимент заканчивается в дату месячного стопа выше.</p>'
slug = '/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08'
body += f'<p><a href="{slug}-uploaded">3 851 запущенный SKU и ставки CSV</a> · <a href="{slug}-deferred">19 SKU для возврата CSV</a> · <a href="{slug}-baseline">База запущенных SKU CSV</a> · <a href="{slug}-guard">Живое состояние проверки JSON</a> · <a href="{slug}-limits">Лимиты JSON</a>.</p>'
script = """<script>fetch('/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08-guard',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}).then(s=>{document.getElementById('guard-live').textContent='Проверка: '+new Date(s.checked_at_utc).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК; '+s.status+'; работающих РК: '+(s.campaigns||[]).filter(c=>c.state==='CAMPAIGN_STATE_RUNNING').length+' из 4; действий остановки: '+(s.actions||[]).length+'. Данные рекламы могут запаздывать.'}).catch(()=>{})</script>"""
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Дисквэр: первая волна запущена</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:25px auto;padding:0 16px}td,th{border:1px solid #ddd;padding:9px;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}#guard-live{padding:14px;background:#edf4fa}</style>' + body + script + '</html>')
(P / 'RESULT.md').write_text('# Дисквэр: первая волна запущена\n\n' + json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'account': 'oz_acc2', 'state': status['state'], 'SKU': len(launched), 'deferred': len(return_rows),
                  'start_msk': start, 'stop_msk': stop, 'guard': guard['status']}, ensure_ascii=False))
