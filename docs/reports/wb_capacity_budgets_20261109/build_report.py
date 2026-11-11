# поток: mkt / WB
"""Offline capacity and low-budget summary; no marketplace writes or calls."""
import collections
import csv
import datetime as dt
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

P = Path(__file__).parent
labels = {4: 'Готова, выключена', 9: 'Работает (статус WB)', 11: 'Пауза'}
rows, accounts, snapshots = [], {}, {}
for account in ['wb_acc1', 'wb_acc2']:
    snapshot = json.loads((P / account / 'snapshot.json').read_text())
    snapshots[account] = snapshot
    campaigns = snapshot['campaigns']
    budgets = {int(b['advertId']): b for batch in snapshot['budget_batches'] for b in batch['adverts']}
    assert len(campaigns) == len({c['id'] for c in campaigns}) == len(budgets)
    assert set(budgets) == {c['id'] for c in campaigns}
    current = []
    for c in campaigns:
        b = budgets[c['id']]
        assert b['currency'] == 'RUB'
        n = len(c['nm_settings'])
        r = {'account': account, 'campaign_id': c['id'], 'name': c['settings']['name'],
             'status': c['status'], 'status_label': labels[c['status']],
             'payment_type': c['settings']['payment_type'], 'bid_type': c['bid_type'],
             'sku_count': n, 'budget_RUB': b['total'], 'budget_le_50': Decimal(str(b['total'])) <= 50,
             'can_change_nms': c.get('restrictions', {}).get('can_change_nms'),
             'subjects': '|'.join(map(str, sorted({nm['subject']['id'] for nm in c['nm_settings']}))),
             'formal_slots_to_50': max(0, 50-n),
             'arithmetic_slots_to_200_UNVERIFIED': max(0, 200-n),
             'add_sku_tested_in_this_audit': False,
             'snapshot_at_utc': snapshot['finished_at_utc']}
        current.append(r); rows.append(r)
    running = [r for r in current if r['status'] == 9]
    cpc = [r for r in running if r['payment_type'] == 'cpc']
    low = [r for r in current if r['budget_le_50']]
    accounts[account] = {
        'campaigns_read': len(current), 'running': len(running), 'running_CPC': len(cpc),
        'running_CPM': len(running)-len(cpc),
        'formal_slots_to_50_running_all': sum(r['formal_slots_to_50'] for r in running),
        'formal_slots_to_50_running_CPC': sum(r['formal_slots_to_50'] for r in cpc),
        'arithmetic_slots_to_200_running_all_UNVERIFIED': sum(r['arithmetic_slots_to_200_UNVERIFIED'] for r in running),
        'arithmetic_slots_to_200_running_CPC_UNVERIFIED': sum(r['arithmetic_slots_to_200_UNVERIFIED'] for r in cpc),
        'low_budget_count': len(low),
        'low_budget_running': sum(r['status'] == 9 for r in low),
        'low_budget_paused': sum(r['status'] == 11 for r in low),
        'low_budget_ready': sum(r['status'] == 4 for r in low)}
for check in json.loads((P / 'budget_legacy_crosscheck.json').read_text()):
    found = next(r for r in rows if (r['account'], r['campaign_id']) == (check['account'], check['campaign_id']))
    assert found['budget_RUB'] == check['budget']['total'], 'Crosscheck changed; keep timestamps and reconcile'
for fn, selected in [('all_campaigns.csv', rows), ('budget_le_50.csv', [r for r in rows if r['budget_le_50']]),
                     ('running_budget_le_50.csv', [r for r in rows if r['budget_le_50'] and r['status'] == 9])]:
    with (P / fn).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(selected)
summary = {'snapshots_at_utc': {a:s['finished_at_utc'] for a,s in snapshots.items()}, 'accounts': accounts,
           'marketplace_mutations_this_task': 0, 'budget_read_source': 'POST /api/advert/v2/budget',
           'budget_currency': 'RUB', 'legacy_budget_crosschecks': 4,
           'capacity_of_old_campaigns_to_200': 'UNKNOWN_NOT_TESTED',
           'capacity_addition_test_performed': False}
(P / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
def table(headers, data):
    return '<div class="scroll"><table><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in data)+'</table></div>'
at = max(dt.datetime.fromisoformat(s['finished_at_utc']) for s in snapshots.values()).astimezone(ZoneInfo('Europe/Moscow'))
body = f'<h1>WB: места в РК и бюджет ≤ 50 ₽</h1><p><b>Обновление 09.11, 22:58 МСК:</b> по уточнённой команде пополнена 21 РК с остатком ≤ 50 ₽, всего на 21 000 ₽; запущены 11 стоявших, включая тестовую X2 41722971. <a href="/api/wb-bids/reports/wb-budget-topup-low50-2026-11-09">Итог пополнения и запуска</a>. Ниже сохранён более ранний срез.</p><p>Проверено {at:%d.%m.%Y %H:%M} МСК. Оба аккаунта, все незавершённые РК в статусах 4/9/11. Только чтение; добавления, ставки, пополнения, запуски и остановки не выполнялись.</p>'
body += '<h2>Места в работающих РК</h2>'
body += table(['Аккаунт', 'Работающих РК', 'Из них CPC / CPM', 'Мест до 50, все РК', 'Мест до 50, CPC', 'Разница до 200, все РК — НЕ ПОДТВЕРЖДЕНО', 'Разница до 200, CPC — НЕ ПОДТВЕРЖДЕНО'],
              [[a, s['running'], f"{s['running_CPC']} / {s['running_CPM']}", s['formal_slots_to_50_running_all'], s['formal_slots_to_50_running_CPC'], s['arithmetic_slots_to_200_running_all_UNVERIFIED'], s['arithmetic_slots_to_200_running_CPC_UNVERIFIED']] for a,s in accounts.items()])
body += '<p><b>В работающих CPC-кампаниях обоих аккаунтов свободных мест до 50 нет.</b> На акк2 исключение — РК22799573 «600»: два SKU, формально48 мест до50, предмет1820, can_change_nms=true. Это unified/CPM, поиск+рекомендации, бюджет0₽, текущая ставка190₽ за1000показов. Она отличается от намеченного CPC-эксперимента. Добавление не проверялось и не выполнялось.</p>'
body += '<p>Доведение старых РК до200 даёт арифметический резерв4103мест на акк1 и925на акк2 (из них727вCPC и198вCPM). Это не подтверждённые доступные места. Ограничение публичного API50 проверено на новой CPC-РК; применимость доведения старых гигантов до200 неизвестна. can_change_nms=true означает возможность изменения состава, а не лимит200. Нужны проверка способа добавления и совместимость предметов; сравнивать резерв с общим числом любых SKU без этих проверок нельзя.</p>'
body += '<h2>Кампании с бюджетом ≤50₽</h2>'
body += table(['Аккаунт', 'Проверено РК', 'Работают', 'На паузе', 'Готовы, выключены', 'Всего ≤50₽'], [[a,s['campaigns_read'],s['low_budget_running'],s['low_budget_paused'],s['low_budget_ready'],s['low_budget_count']] for a,s in accounts.items()])
low = sorted([r for r in rows if r['budget_le_50']], key=lambda r:(r['account'], {9:0,11:1,4:2}[r['status']], Decimal(str(r['budget_RUB'])),r['campaign_id']))
body += table(['Аккаунт', 'ID', 'Название', 'Статус', 'Оплата', 'SKU', 'Остаток, ₽'], [[r['account'],r['campaign_id'],r['name'],r['status_label'],r['payment_type'].upper(),r['sku_count'],r['budget_RUB']] for r in low])
body += '<p>Бюджет — остаток внутри конкретной РК, не дневной лимит и не общий баланс кабинета. Статус9 получен из WB и сам по себе не подтверждает текущие показы при нулевом бюджете. Тестовая РК41722971 акк1 выделена как готовая и выключенная: её нулевой бюджет ожидаем. Деньги автоматически не добавлялись.</p>'
slug='/api/wb-bids/reports/wb-capacity-budgets-2026-11-09'
body += f'<p><a href="{slug}-all">Все169РК CSV</a> · <a href="{slug}-low">Все21РК с бюджетом≤50 CSV</a> · <a href="{slug}-running-low">10работающих с бюджетом≤50 CSV</a> · <a href="{slug}-data">Сводка JSON</a></p>'
body += '<p>Следующий шаг: отдельно решить пополнение десяти работающих РК с малым остатком; для включения нового ассортимента сначала подтвердить кабинетный способ расширения одной старой РК. В рамках этой проверки никаких записей не выполнялось.</p>'
body += '<p>Источник остатков: <a href="https://dev.wildberries.kg/docs/openapi/promotion">официальный метод WB «Остатки бюджетов кампаний»</a> POST /api/advert/v2/budget; покрытие169/169, четыре суммы дополнительно совпали с прежним GET /adv/v1/budget.</p>'
(P/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WB: места в РК и низкие бюджеты</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border:1px solid #ddd;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}</style>'+body+'</html>')
print(json.dumps(accounts,ensure_ascii=False))
