# поток: mkt / WB
"""Publish recovery evidence offline; no transfers or messages."""
import datetime as dt
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

P = Path(__file__).parent
root = P.parents[2]
status = json.loads((root / 'docs/reports/mkt_ad_budget_guard_latest.json').read_text())
timer = json.loads((P / 'timer_state.json').read_text())
at = dt.datetime.fromisoformat(status['at_utc']).astimezone(ZoneInfo('Europe/Moscow'))
initial = 'https://github.com/sergkidman-ai/mp-analytics/commit/35c57e474661803ad358e18bab0c4737f8255f55'
manual = 'https://github.com/sergkidman-ai/mp-analytics/commit/c5e1725eaa27276f441a00bd3651e3ec2623f6e6'
body = f'<h1>WB: восстановление контроля бюджетов</h1><p><b>Обновление 09.11, 22:58 МСК:</b> по уточнённой команде пополнена 21 РК с остатком ≤ 50 ₽, всего на 21 000 ₽; запущены 11 стоявших, включая тестовую X2 41722971. <a href="/api/wb-bids/reports/wb-budget-topup-low50-2026-11-09">Итог пополнения и запуска</a>. Ниже сохранён более ранний срез.</p><p>Первый проверенный запуск: {at:%d.%m.%Y %H:%M} МСК.</p>'
body += f'<p><b>В найденном коде GitHub был настроен контроль с рекомендациями, без автоматического пополнения.</b> Это прямо зафиксировано в <a href="{initial}">коммите18.09</a>. <a href="{manual}">Пополнение12.10</a> было отдельной разовой операцией:30РК по1000₽. Восстанавливать постоянное автоматическое списание по этой разовой операции оснований нет.</p>'
body += '<h2>Что восстановлено</h2><ul><li>Ежедневная проверка обоих WB: историческое расписание07:30UTC /10:30МСК.</li><li>Установлены wb-ad-budget-guard.service и .timer; таймер enabled/active, первый запуск завершён успешно.</li><li>Применён актуальный метод чтения бюджетов POST /api/advert/v2/budget. Он не переводит деньги; прежний GET /adv/v1/budget отключается16.11.</li><li>Добавлена защита от ошибочного вывода при пустой/устаревшей статистике. Неизвестные заказы не считаются нулевыми, суммы пополнения по ним не предлагаются.</li><li>Режим --dry: локальный отчёт и BI; Telegram выключен. Автопополнение выключено, денежных операций0.</li></ul>'
body += '<h2>Текущий результат</h2><table><tr><th>Аккаунт</th><th>CPC прочитано</th><th>История недоступна</th><th>Работающих с остатком&lt;50₽</th></tr>'
for account, rows in status['accounts'].items():
    low = [r for r in rows if r['status'] == 9 and r['bal'] is not None and r['bal'] < 50]
    body += f'<tr><td>{escape(account)}</td><td>{len(rows)}</td><td>{sum(r["statistics_state"] != "CURRENT" for r in rows)}</td><td>{len(low)}</td></tr>'
body += '</table><p>Состояние DEGRADED: остатки читаются, но wb_ad_nm_daily на новом сервере пуста. Окупаемость и рекомендуемые суммы по прежним правилам определить нельзя. CPM-кампания22799573 не входит в историческую CPC-политику; её нулевой бюджет отражён в отдельном отчёте обоих аккаунтов.</p>'
body += '<h2>Какие правила действительно были</h2><p>Выгодной считалась РК с минимум2рекламными заказами за28дней и CPO≤200₽. Сигнал — остаток&lt;50₽ или менее3дней при среднем расходе последних7дней. Рекомендуемая сумма — расход28дней, округлённый вверх до100₽, минимум1000₽. Это рекомендации для решения владельца, не автоматическая операция.</p>'
body += '<h2>Что нужно для автоматического пополнения</h2><p>Сохранённой политики автоматических переводов не найдено. Для нового денежного режима нужны порог остатка, сумма одного пополнения, общий недельный лимит каждого аккаунта и источник денег — Счёт или взаимозачёт. Для сохранения прежнего отбора выгодных РК также нужно восстановить рекламную историю. Параметры запрошены у владельца; до ответа автопереводы не включаются.</p>'
body += f'<p>Следующее расписание UTC: {escape(timer["NextElapseUSecRealtime"])}. Проверки:8тестов прошли, реальный systemd-запуск успешен. Дублирующего cron/таймера этой задачи не найдено.</p>'
slug = '/api/wb-bids/reports/wb-budget-guard-recovery-2026-11-09'
body += f'<p><a href="{slug}-status">Текущий статус JSON, обновляется расписанием</a> · <a href="{slug}-text">Текущий текстовый отчёт</a> · <a href="/api/wb-bids/reports/wb-capacity-budgets-2026-11-09">Все РК с малым бюджетом</a></p>'
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WB: восстановление контроля бюджетов</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
print('Rendered recovery report: read-only timer restored; missing history explicit; no automatic transfers or Telegram')
