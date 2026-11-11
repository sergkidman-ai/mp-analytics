# поток: mkt / ozon
"""Offline publication of verified W1 creation, actual Ozon required budgets and audit."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import html
import json
from pathlib import Path

O = Path(__file__).parent
P = O.parent
load = lambda p: json.loads(p.read_text())
if (P / 'activation_status.json').exists() and load(P / 'activation_status.json').get('state') == 'WAVE1_RUNNING_CONFIRMED':
    import runpy
    runpy.run_path(str(P / 'activation_20261109/publish_live.py'), run_name='__main__')
    raise SystemExit(0)
save = lambda p, z: p.write_text(json.dumps(z, ensure_ascii=False, indent=2) + '\n')
binding = load(O / 'created_campaign_bindings.json')
budget = load(O / 'ozon_required_budgets.json')
draft = load(P / 'experiment_DRAFT.json')
limit_config = load(P / 'risk_policy.json') if (P / 'risk_policy.json').exists() else {}
limit_install = load(P / 'limits_from_acc1/installation_status.json') if (P / 'limits_from_acc1/installation_status.json').exists() else {}
guard_installed = bool(limit_install.get('guard_installed') and limit_install.get('timer_enabled') and limit_install.get('timer_active'))
assert binding['account'] == budget['account'] == draft['account'] == 'oz_acc2'
assert binding['all_four_inactive_verified'] and len(binding['campaigns']) == 4
assert not binding['actual_activation_events'] and not binding['activation_allowed']
ids = {x['campaign_id'] for x in binding['campaigns']}
forbidden = set(binding['forbidden_existing_campaign_ids'])
assert not ids & forbidden
journal = [json.loads(x) for x in (O / 'mutation_journal.jsonl').read_text().splitlines()]
intents = [x for x in journal if x['phase'] == 'INTENT']
responses = [x for x in journal if x['phase'] == 'RESPONSE']
assert len(intents) == len(responses) == 44
assert sum(x['path'] == '/api/client/campaign/cpc/v2/product' for x in intents) == 4
assert all(x['http'] == 200 for x in responses)
assert all('/activate' not in x['path'] for x in intents)
assert all(x['path'] == '/api/client/campaign/cpc/v2/product' or x['path'].split('/')[4] in ids for x in intents)
assert not load(O / 'existing_campaign_integrity.json')['differences']
rows = list(csv.DictReader((O / 'preflight/fresh_eligibility.csv').open(encoding='utf-8-sig')))
lookup = {c['plan_key']: c for c in binding['campaigns']}
uploaded = []
floor_counts = collections.Counter()
for x in rows:
    assert x['account'] == 'oz_acc2' and x['eligible_for_upload'] == 'True'
    c = lookup[x['campaign_key']]
    floor_counts[x['bid_rub']] += 1
    uploaded.append({**x, 'actual_campaign_id': c['campaign_id'], 'actual_campaign_state': c['verified_state'],
                     'bid_verified_at_utc': c['verified_at_utc'], 'actual_activation_at': '',
                     'native_budget_verified_for_acc2': 'True', 'automatic_application_allowed': 'False',
                     'activation_called': 'False'})
assert len(uploaded) == len({x['sku'] for x in uploaded}) == 3870
for c in binding['campaigns']:
    z = load(O / c['readback_file'])
    assert z['campaign']['state'] == 'CAMPAIGN_STATE_INACTIVE'
    wanted = {x['sku']: x for x in uploaded if x['actual_campaign_id'] == c['campaign_id']}
    assert set(wanted) == {str(x['sku']) for x in z['products']}
    assert all(D(str(x['bid'])) == D(wanted[str(x['sku'])]['bid_micro_rub']) for x in z['products'])
with (O / 'uploaded_wave1.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(uploaded[0]))
    w.writeheader()
    w.writerows(uploaded)
status = {'account': 'oz_acc2', 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
          'source_draft_sha256': hashlib.sha256((P / 'experiment_DRAFT.json').read_bytes()).hexdigest(),
          'created_W1_campaigns': 4, 'uploaded_W1_SKU': 3870, 'all_inactive_verified': True,
          'actual_activation_at': None, 'actual_stop_at': None, 'activation_events': [],
          'total_Ozon_required_weekly_budget_rub': budget['total_required_weekly_budget_rub'],
          'owner_actual_weekly_spend_authorized_rub': limit_config.get('owner_weekly_reference_rub'),
          'owner_weekly_reference_is_hard_cap': False, 'individual_minimum_histogram': dict(floor_counts),
          'account2_guard_installed': guard_installed, 'account2_month_stop_installed': False,
          'preexisting_campaigns_changed': 0, 'W2_campaigns_created': 0,
          'month_rule': draft['month_rule'],
          'children_with_existing_shared_model_in_W1': ['4148797634', '4148802580'],
          'child_model_card_mutations': 0, 'new_family_based_exclusions': 0,
          'baseline': 'provisional 2026-10-11..2026-11-07; refresh missing full days and delayed orders relative to actual D0 before activation'}
save(P / 'execution_status.json', status)
save(O / 'validation.json', {'validated_at_utc': status['at_utc'], 'status': 'PASS',
     'all_four_inactive': True, 'exact_SKU_composition_and_minimum_bids': True,
     'mutation_intents_responses': [len(intents), len(responses)],
     'writes': {'create_new_inactive': 4, 'add_minimum_bid_batches': 40, 'activate': 0},
     'dynamic_budget_calculation_reads': 4, 'old_campaign_settings_unchanged': True})
archive = P / 'before_creation'
archive.mkdir(exist_ok=True)
for name in ['index.html', 'RESULT.md', 'HANDOFF.md']:
    if not (archive / name).exists():
        (archive / name).write_bytes((P / name).read_bytes())


def money(v):
    return f'{int(D(str(v))):,}'.replace(',', ' ')


def table(head, data):
    return '<table><tr>' + ''.join('<th>' + html.escape(str(x)) + '</th>' for x in head) + '</tr>' + ''.join(
        '<tr>' + ''.join('<td>' + html.escape(str(x)) + '</td>' for x in r) + '</tr>' for r in data) + '</table>'


labels = {'wave1_children': 'Дочерние', 'wave1_new_6months': 'Новинки за полгода', 'wave1_old': 'Старые', 'wave1_bundles_x3': 'Бандлы X3'}
order = list(labels)
budgets = {x['segment']: x for x in budget['campaigns']}
body = '<h1>Акк2 · волна 1 создана, реклама не активирована</h1>'
body += '<p><b>Четыре новые РК заполнены, 3 870 SKU. Все выключены.</b> Состав каждой РК и индивидуальная минимальная ставка каждого SKU подтверждены обратным чтением API. Активаций 0; существующие РК не менялись. Состояние: 8 ноября 2026, около 23:15 МСК; точные времена сохранены в журналах.</p>'
body += '<h2>Фактический ответ Ozon по бюджету</h2>'
body += table(['Кампания', 'ID', 'SKU', 'Мин. CPC, ₽', 'Требуемый недельный бюджет, ₽', 'Статус'], [
    [labels[k], budgets[k]['campaign_id'], budgets[k]['SKU_count'],
     ' / '.join(sorted({x['bid_rub'] for x in uploaded if x['campaign_key'] == k}, key=D)),
     money(budgets[k]['Ozon_required_weekly_budget_rub']), 'Выключена'] for k in order] +
    [['ИТОГО', '', len(uploaded), '', money(budget['total_required_weekly_budget_rub']), '0 активаций']])
body += '<p>Источник: четыре ответа <code>POST /external/api/dynamic_budget</code>, тип <code>DYNAMIC_BUDGET_REQUIRED</code>, по созданным ID. Ozon вернул 2 000 ₽ на SKU в неделю, всего 7 740 000 ₽. Это технические недельные бюджеты кампаний, не фактически потраченная сумма. Общий рабочий лимит расходов акк2 пока не согласован. Ориентир 12 500 ₽ акк1 сюда не переносился.</p>'
if guard_installed:
    body = body.replace('Общий рабочий лимит расходов акк2 пока не согласован. Ориентир 12 500 ₽ акк1 сюда не переносился.', 'По новой команде владельца для акк2 приняты те же лимиты, что у акк1: 12 500 ₽ на первую волну за неделю. Это мягкий ориентир с сигналом и оценкой при превышении; технические бюджеты Ozon остаются прежними.')
body += '<p>Ставки прочитаны заново перед загрузкой: 7 ₽ — 167 SKU; 8 ₽ — 351; 10 ₽ — 3 352. Все 3 870 сохранили индивидуальный допуск: наличие, неархивность, доступный канал, цена, отсутствие ошибок и отсутствие в другой работающей CPC РК. Родители и родственники не служили фильтром.</p>'
body += '<h2>Остальная часть месячного плана</h2>'
body += '<p>Волна 2 пока не создана: 3 918 фиксированных SKU — старые 715, X6 1 326, X9 1 032, X12 845. Дополнительно — накопленные восстановленные дочерние по свежей проверке перед второй волной, без пересечений с первой и работающей CPC рекламой.</p>'
body += '<p>План: W2 через 7 дней после фактического D0; общий период — один календарный месяц от фактической активации W1. D0 и остановка пока не назначены. Это не продление эксперимента акк1. Связанные бандлы разных кратностей попадают в разные волны: 1 392 модели в исходном плане пересекают волны. Поэтому сравнение не является независимым контролируемым причинным тестом.</p>'
body += '<h2>Как будем измерять</h2>'
body += '<p>Основной приоритет — профиль аккаунта: общая заказанная стоимость, заказы, расходы деньгами/бонусами, CPC/CPO отдельно. Затем каждый SKU и отдельно фактическая Ozon-склейка основных товаров/бандлов. Связь по первым четырём цифрам артикула — дополнительная товарная семья для анализа, не доказательство Ozon-склейки. Дочерние по замыслу являются отдельными карточками.</p>'
body += '<p>Предварительная база за 11 октября–7 ноября: 348 уникальных неотменённых заказов, 3 302 105 ₽ заказанной стоимости; это не финансовая реализация или прибыль. База взята из уже сохранённых данных акк2; до запуска её нужно привязать к реальному D0 и обновить недостающие полные дни и задержанные заказы. Общее CPO ALL_SKU действует фоном: проверяем добавление CPC поверх CPO.</p>'
body += '<p>На акк2 подтверждена одна существующая склейка дочерних: 09278T3Z11SY + 09279QTTL6OR, model_id 5232972115, SKU 4148797634 + 4148802580. Обе доступны и заведены в РК дочерних первой волны; общий model_id уже был в исходном каталоге, рекламная загрузка его не создавала. Карточки/склейки не менялись. Для этих двух измерение собственного SKU и пары нужно различать.</p>'
body += '<h2>Что осталось до активации</h2>'
body += '<p>Согласовать фактический рабочий лимит расходов и правила остановки для акк2, обновить базу, установить отдельную проверку каждые 3 часа и месячную остановку, затем активировать по прямой команде владельца. Сейчас guard акк2 и месячный автостоп не установлены. Защита акк1 на эти РК не распространяется.</p>'
body += '<p>Проект порога 1 000 ₽/SKU без подтверждённых собственных и связанных заказов остаётся проектом для акк2; остальные риски — сигналы. Способ выключения с сохранением SKU и последствия недельного технического бюджета должны быть зафиксированы до запуска.</p>'
if guard_installed:
    body = body.replace('<h2>Что осталось до активации</h2>', '<h2>Приняты лимиты акк1; проверка акк2 установлена</h2>')
    body = body.replace('Согласовать фактический рабочий лимит расходов и правила остановки для акк2, обновить базу, установить отдельную проверку каждые 3 часа и месячную остановку, затем активировать по прямой команде владельца. Сейчас guard акк2 и месячный автостоп не установлены. Защита акк1 на эти РК не распространяется.', 'Согласованы лимиты на основе действующей конфигурации акк1. Для акк2 установлен отдельный guard каждые 3 часа, только на четыре новые РК. Первый запуск успешен: все четыре выключены, изменений рекламы и внешних сообщений 0. Конфигурация и защита акк1 не менялись. До активации остаётся обновить базу и закрепить месячную остановку по фактическому D0; запуск рекламы этой командой не разрешён.')
    body = body.replace('Проект порога 1 000 ₽/SKU без подтверждённых собственных и связанных заказов остаётся проектом для акк2; остальные риски — сигналы. Способ выключения с сохранением SKU и последствия недельного технического бюджета должны быть зафиксированы до запуска.', 'Сигнал от 300 ₽/SKU без заказов; при недоступных данных заказов — от 500 ₽. Автоматическая пауза только от 1 000 ₽/SKU при подтверждённом отсутствии собственных, модельных и семейных заказов. Пауза всей затронутой новой РК с сохранением всех SKU; удалений и автоматического включения нет. Окно проверки как на акк1: текущий и предыдущий календарные дни МСК, пересчёт каждые 3 часа. Неизвестные заказы не считаются нулём, потеря данных вызывает только сигнал с повтором не чаще 6 часов. Превышение недельного ориентира — сигнал, затем повтор после дополнительных 25% ориентира. Сигналы доступны в BI и локальном журнале; Telegram этой командой не подключался. Месяц — от фактического старта акк2, дата и отдельный таймер остановки пока не установлены.')
    body += '<p><a href="/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08-guard">Текущее состояние автоматической проверки JSON</a> · <a href="/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08-limits">Принятые лимиты JSON</a>.</p>'
slug = '/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08'
body += f'<p><a href="{slug}-budgets">Фактические бюджеты CSV</a> · <a href="{slug}-uploaded">Загруженные SKU, ставки и ID РК CSV</a> · <a href="{slug}-wave2">Проект волны 2 CSV</a> · <a href="{slug}-baseline">Предварительная база CSV</a> · <a href="/api/wb-bids/reports/ozon-model-map-comparison-2026-11-08">Исправленное сравнение склеек без дочерних</a>.</p>'
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Акк2: волна 1 создана и выключена</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:25px auto;padding:0 16px}td,th{border:1px solid #ddd;padding:9px;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>' + body + '</html>')
(P / 'RESULT.md').write_text('# Акк2: первая волна создана и выключена\n\n4 РК, 3 870 SKU. Индивидуальные минимумы и состав проверены чтением API. Активаций 0.\n\n' + json.dumps(status, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'validation': 'PASS', 'campaigns': 4, 'SKU': len(uploaded),
                  'Ozon_required_weekly_budget_rub': budget['total_required_weekly_budget_rub'], 'activated': 0}, ensure_ascii=False))
