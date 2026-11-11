# поток: mkt / WB
"""Render the completed capacity probe from saved responses, without API calls."""
import csv
import datetime as dt
import html
import json
from pathlib import Path

P = Path(__file__).parent
load = lambda name: json.loads((P / name).read_text())
final = load('independent_final_readback.json')
v = final['validation']
assert not v['old_status_membership_bids_changed']
assert v['new_test_status'] == 4 and v['new_test_budget'] == 0
assert v['new_ids_since_preflight'] == [41722971]
assert v['new_test_sku_count'] == 50
campaign = next(c for c in final['campaigns'] if c['id'] == 41722971)
floors = {int(n['nm_id']): next(b['value'] for b in n['bids'] if b['type'] == 'search')
          for n in load('new_test_minimum_bids_readback.json')['response']['bids']}
assert len(floors) == 50
assert all(n['bids_kopecks']['search'] == floors[n['nm_id']] for n in campaign['nm_settings'])
packing = load('packing_summary.json')
for scenario in packing:
    rows = list(csv.DictReader((P / f"packing_remaining_{scenario['capacity']}_DRAFT.csv").open(encoding='utf-8-sig')))
    assert len(rows) == len({r['nm_id'] for r in rows}) == 4769
    groups = {}
    for r in rows:
        groups.setdefault(r['draft_campaign_key'], []).append(r)
    assert len(groups) == scenario['extra_campaigns']
    assert all(len(g) <= scenario['capacity'] and len({r['subject_id'] for r in g}) == 1 for g in groups.values())
pool = list(csv.DictReader((P / 'test_pool_800.csv').open(encoding='utf-8-sig')))[:50]
with (P / 'test_campaign_50.csv').open('w', encoding='utf-8-sig', newline='') as f:
    rows = [{**r, 'campaign_id': 41722971, 'campaign_status': 4,
             'bid_RUB': f"{floors[int(r['nm_id'])]/100:.2f}", 'budget_RUB': 0} for r in pool]
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
summary = {'account': 'wb_acc1', 'verified_at_utc': final['at_utc'],
           'session_id': '01a1a820-d5a8-75d3-a20c-6be4e462a2f5',
           'campaign_id': 41722971, 'status': 4, 'payment_type': 'cpc',
           'sku_count': 50, 'budget_RUB': 0, 'all_at_native_floor': True,
           'native_floor_RUB': 7.30, 'activation_calls': 0, 'deposit_calls': 0,
           'existing_campaign_writes': 0, 'old_campaigns_verified_unchanged': 93,
           'add_50_to_make_100': 'HTTP400_TOTAL_LIMIT_50',
           'create_200': 'HTTP400_TOTAL_LIMIT_50', 'create_300': 'NOT_ATTEMPTED',
           'exact_cabinet_workflow': 'NOT_TESTED_NO_CONNECTED_CABINET',
           'old_campaigns_over_50': 91, 'maximum_old_campaign_size': 200,
           'packing': packing, 'paid_month_experiment_started': False,
           'WB_monitor_or_autostop_installed': False}
(P / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
slug = '/api/wb-bids/reports/wb-acc1-capacity-probe-2026-11-09'
body = '''<h1>WB акк1: тест вместимости рекламной кампании</h1><p><b>Обновление 09.11, 22:58 МСК:</b> по уточнённой команде пополнена 21 РК с остатком ≤ 50 ₽, всего на 21 000 ₽; запущены 11 стоявших, включая тестовую X2 41722971. <a href="/api/wb-bids/reports/wb-budget-topup-low50-2026-11-09">Итог пополнения и запуска</a>. Ниже сохранён более ранний срез.</p>
<p><b>Итог: через проверенный публичный API новая CPC-кампания ограничена 50 SKU.</b>
Создана одна тестовая РК 41722971, «TEST_API_X2_STEP50_20261109»: 50 бандлов X2 с положительным свежим FBS-остатком,
ручные ставки, поиск, оплата за клики. Все 50 ставок совпадают с индивидуальным минимумом WB — 7,30 ₽.</p>
<p><b>Статус 4 — готова к запуску, выключена. Бюджет 0 ₽.</b> Запусков и пополнений не было.
Отдельным чтением подтверждено, что статусы, составы и ставки всех 93 прежних РК не изменились.</p>
<table><tr><th>Проверка</th><th>Результат</th></tr>
<tr><td>Создание CPC на 50 SKU</td><td>Успех, состав 50 подтверждён чтением</td></tr>
<tr><td>Добавление ещё 50 после создания</td><td>HTTP 400: общий лимит 50; состав остался 50</td></tr>
<tr><td>Сбор списка 200 партиями по 50 до отправки, затем один Create</td><td>HTTP 400: не более 50 товаров в кампании; вторая РК не создана</td></tr>
<tr><td>Создание на 300</td><td>Не выполнялось после явного отказа на 200</td></tr></table>
<p>Сбор списка перед Create выполнен на стороне скрипта. Это <b>не воспроизведение внутренних запросов кабинета</b>.
В текущей сессии нет подключённого браузера с кабинетом WB. Поэтому кабинетный способ «добавить 50, ещё 50, затем Создать»
остаётся непроверенным. В существующих РК действительно есть 91 кампания более чем на 50 SKU, до 200 включительно;
их текущий тип manual/CPC не доказывает, каким способом они создавались. Последняя такая РК создана 07.08.2026.
Утверждать, что крупные кампании вообще невозможны, оснований нет.</p>
<p>Для теста использованы актуальные имена полей API bid_type/payment_type/placement_types:
создание именно CPC подтверждено ответом WB. Старый массовый скрипт использует другую запись параметров;
он не разблокирован и не запускался.</p>
<h2>Сколько кампаний потребуется</h2>
<p>Исходный срез: 5 616 товаров с подтверждённым остатком вне работающей рекламы.
Из них 797 уже находятся в приостановленных РК — их нужно рассматривать отдельно.
Ещё 4 819 не было в незавершённых РК. Теперь 50 из них в выключенном тесте, остаётся 4 769.
Работающей рекламы у этих 5 616 по-прежнему нет. Числа относятся к сохранённому срезу 9 ноября;
свежий остаток перед тестом перечитан только для пула 800 бандлов, перед массовым запуском остатки нужно актуализировать.</p>
<table><tr><th>SKU на РК</th><th>Ещё РК на 4 769 SKU</th><th>Вместе с тестовой РК</th><th>Подтверждение</th></tr>'''
for s in packing:
    body += f"<tr><td>{s['capacity']}</td><td>{s['extra_campaigns']}</td><td>{s['total_including_test']}</td><td>{'Проверено на новом тесте' if s['capacity']==50 else 'Только сценарий; вместимость не подтверждена'}</td></tr>"
body += '''</table><p>Раскладки разделяют бандлы X2/X6/X10, дочерние, новинки и старые товары, а также предметы WB.
Это черновики, распределение не применено. Наличие родителя не является условием допуска дочернего товара.</p>
<h2>Следующий конкретный шаг</h2>
<p>Проверить кабинетный способ на одном выключенном черновике X2: последовательный выбор 4×50 до кнопки «Создать»,
без пополнения и запуска. Затем прочитать фактические 200 SKU по ID через API и сверить тип оплаты.
Если кабинет примет 200, расширить отдельный тест до 300; если нет — рабочий подтверждённый вариант по 50.</p>
<p>До принятия схемы вместимости массовое создание не выполнялось. Для платного месячного эксперимента ещё нужны
согласованный бюджет WB и правила остановки; лимиты Ozon автоматически не переносятся.
Никакая очистка и новые снижения ставок сейчас не выполняются.</p>
<h2>Месяц наблюдения до чистки</h2>
<p>Период считать от фактического начала показов, а не от подготовки РК. Снять 28 полных дней до запуска;
после запуска измерять показы, клики, расход, рекламные заказы, общие заказы и их стоимость отдельно по SKU,
фактическому imtID и всему WB акк1. Артикульная семья — отдельный дополнительный уровень, не склейка.
Перечитывать задержанные заказы и отмены, сравнивать одинаковые дни недели, фиксировать цены, акции и наличие.</p>
<p>Без контрольной группы изменение аккаунта будет наблюдаемой динамикой, а не доказанным эффектом рекламы.
Изменение цен акции 11.11 необходимо учитывать отдельно. Трёхчасовой мониторинг и автостоп WB пока не установлены;
месячный рекламный эксперимент WB не запущен.</p>'''
body += f'<p>Проверено UTC: {html.escape(final["at_utc"])}. <a href="{slug}-skus">50 тестовых SKU CSV</a> · <a href="{slug}-data">Сводка JSON</a></p><p>'
body += ' · '.join(f'<a href="{slug}-packing-{n}">Раскладка {n} SKU/РК — DRAFT CSV</a>' for n in [50,200,300])
body += '</p><p><a href="https://dev.wildberries.kg/docs/openapi/promotion">Официальная документация WB API</a>: создание CPC с минимальными ставками, лимит Create 50.</p>'
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WB акк1: тест 50/200/300</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}a{color:#155ab6}</style>' + body + '</html>')
print('Rendered: new CPC inactive50, budget0, old93 unchanged; packing 100/31/24 extra RK; no paid launch')
