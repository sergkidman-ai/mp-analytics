# поток: mkt / WB
"""Render verified payment and activation results without API calls."""
import csv
import datetime as dt
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

P = Path(__file__).parent
verified = json.loads((P / 'verification.json').read_text())
assert verified['status'] == 'PASS' and verified['total_added_RUB'] == 21000
assert verified['all_selected_status9'] and verified['every_pretransfer_budget_le50']
rows = list(csv.DictReader((P / 'verified_result.csv').open(encoding='utf-8-sig')))
assert len(rows) == len({(r['account'],r['campaign_id']) for r in rows}) == 21
assert all(int(r['after_status']) == 9 and int(r['added_RUB']) == 1000 for r in rows)
at = dt.datetime.fromisoformat(verified['verified_at_utc']).astimezone(ZoneInfo('Europe/Moscow'))


def table(headers, data):
    return '<div class="scroll"><table><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in data)+'</table></div>'


body = f'<h1>WB: пополнение РК с остатком ≤ 50 ₽</h1><p>Операция 09.11.2026. Итог проверен {at:%d.%m.%Y %H:%M:%S} МСК независимым чтением API.</p>'
body += '<p><b>В 21 РК добавлено по 1 000 ₽, всего 21 000 ₽. Запущены 11 ранее выключенных РК; все 21 выбранная кампания имеют статус 9 — работает.</b> Пополнены только РК с остатком ≤ 50 ₽ перед переводом. Остальные 148 РК не пополнялись.</p>'
body += table(['Аккаунт','Пополнено РК','Добавлено, ₽','Ранее выключенных запущено','Выбранных со статусом9'], [[a,s['funded_campaigns'],s['deposited_RUB'],s['start_calls_HTTP200'],s['selected_final_running']] for a,s in verified['accounts'].items()])
body += '<p>Это добавление 1 000 ₽ к прежнему остатку. Остатки после операции могут отличаться от 1 000 ₽: в работающих кампаниях уже идёт расход. Зачисления проверены ответами WB, повторным чтением бюджетов и точным уменьшением средств счёта: 14 000 ₽ на акк1 и 7 000 ₽ на акк2.</p>'
body += '<p>На момент итоговой проверки во всём акк1 работали 94 РК, в акк2 — 75. Дополнительные 148 РК уже работали до этой операции и денег в рамках неё не получали.</p>'
body += '<h2>Полный список</h2>'
body += table(['Аккаунт','ID РК','Название','Оплата','Остаток при отборе, ₽','Добавлено, ₽','Остаток при итоговой проверке, ₽','До → после'], [[r['account'],r['campaign_id'],r['name'],r['payment_type'].upper(),r['budget_before_preflight_RUB'],r['added_RUB'],r['final_budget_RUB'],f"{r['before_status']} → {r['after_status']}"] for r in rows])
test = next(r for r in rows if r['campaign_id']=='41722971')
body += f'<p>В число запущенных вошла ранее тестовая РК 41722971 с 50 бандлами X2 на акк1. Фактическое поле started WB: {escape(test["actual_started_at"])}. Её активация разрешена уточнённой командой пополнить и запустить РК с малым остатком; прежний статус выключенного теста относится к более раннему срезу. Одна уже работающая CPM-кампания 22799573 акк2 также получила 1 000 ₽, тип оплаты не менялся.</p>'
body += '<h2>Проверка выполнения</h2><p>Для трёх кампаний акк1 первый ответ запуска HTTP 200 не сопровождался переходом в работу. После независимой проверки паузы выполнен один повтор только запуска для 35697852/35698099/35699026. Пополнения не повторялись. Финальное чтение подтвердило статус 9 у всех трёх.</p>'
body += f'<p>Повторных успешных переводов — 0, изменений состава — 0, наблюдаемых изменений ставок — {len(verified["observed_bid_changes"])}. Отменённый план пополнить 169 РК на 169 000 ₽ не исполнялся. В работе использован отдельный фиксированный список 21 РК и журнал намерений/ответов с проверкой суммы каждого перевода.</p>'
body += '<p>Разовое поручение выполнено. Автопополнение по расписанию не включалось. Ежедневный контроль бюджетов остаётся в режиме чтения; рекламная история вБД пока отсутствует, поэтому оценка окупаемости сторожем недоступна. Наличие показов и заказов не выводится из одного статуса9.</p>'
slug='/api/wb-bids/reports/wb-budget-topup-low50-2026-11-09'
body += f'<p><a href="{slug}-csv">Итог21РК CSV</a> · <a href="{slug}-data">Независимая проверка JSON</a> · <a href="/api/wb-bids/reports/wb-budget-guard-recovery-2026-11-09-status">Текущий статус ежедневного контроля</a></p>'
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WB: пополнены21РК</title><style>body{font:16px/1.55 system-ui;max-width:1250px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}</style>'+body+'</html>')
print('Rendered verified result:21×1000=21000RUB;11starts;all21RUNNING;no repeats or bid/composition changes')
