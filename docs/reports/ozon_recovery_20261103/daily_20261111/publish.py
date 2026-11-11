# поток: mkt / ozon
"""Publish the existing fixed20 refresh; no API calls or database writes."""
import csv
from decimal import Decimal as D
from html import escape
import json
from pathlib import Path

P=Path(__file__).parent
S=json.loads((P/'status.json').read_text());b,a=S['periods']
money=lambda n:f'{D(str(n)):,.2f}'.replace(',',' ').replace('.',',')
whole=lambda n:f'{int(D(str(n))):,}'.replace(',',' ')
pct=lambda n:f'{D(str(n)):+.1f}%'.replace('.',',') if n is not None else '—'


def table(head,rows):
    return '<div class="scroll"><table><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in head)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</table></div>'


body='<h1>Фиксированные 20 SKU Ozon: полные дни по 10 ноября</h1><p><b>Расход по полученным строкам снизился на 83,4%, ДРР Ozon — с 46,0% до 10,4%. Одновременно общие заказы самих 20 SKU сократились с 70 до 47, стоимость заказов — на 22,4%.</b> Это наблюдаемая динамика после снижения, причинный эффект ставок отдельно не выделен.</p>'
body+='<p>Обновление 11.11.2026: существующие команды fetch_monitor20/analyze_monitor20, пересчитаны заказы и отмены 31.10–10.11. Текущий 11 ноября исключён как неполный, 30 октября — переходный день. Ставки и рекламные настройки не менялись.</p>'
body+='<h2>Равные периоды по 11 полных дней</h2>'
display=[]
for r in [b,a]:
    display.append([r['from_msk']+' — '+r['through_msk'],money(r['ad']['spend_rub']),money(r['average_CPC_RUB']),money(r['ad_DRR_percent'])+'%',r['sales']['own']['orders'],money(r['sales']['own']['ordered_value_RUB']),r['sales']['models']['orders'],money(r['sales']['models']['ordered_value_RUB'])])
body+=table(['Период МСК','Расход по отчётным строкам, ₽','Средний CPC, ₽','ДРР Ozon','Общие заказы 20 SKU','Стоимость 20 SKU, ₽','Заказы 19 склеек','Стоимость склеек, ₽'],display)
body+='<p>Разница наблюдаемого расхода: −138 684,90 ₽. Разница стоимости заказов самих SKU: −134 678 ₽; склеек: −169 930 ₽. Стоимость заказов и рекламные расходы — разные составляющие, их простое сравнение не является расчётом прибыли.</p>'
body+='<p>Расход суммирован по всем полученным размещениям, без деления на две кампании. Известны 36 связок SKU–РК в шести CPC-кампаниях. До в выбранном окне наблюдались 32 связки, после — 34; за всю сохранённую историю есть строки по 35 связкам. Полнота истории всех 36 не доказана. Пропуски не считались нулём.</p>'
body+='<h2>Поправка на дни недели и выбор базы</h2><p>В двух 11-дневных периодах различается состав дней недели. Отдельный расчёт сравнивает каждый день после снижения со средним таких же дней недели за 02–29 октября.</p>'
matched={}
for r in S['weekday_matched28_general_sales']:matched.setdefault(r['scope'],{})[r['metric']]=r
labels={'fixed20':'20 SKU','families_union':'19 склеек','cabinet':'Весь аккаунт'}
body+=table(['Уровень','Заказов ожидалось','Заказов фактически','Δ заказов','Стоимость ожидалась, ₽','Стоимость фактически, ₽','Δ стоимости'],[[labels[k],money(v['orders']['expected_same_weekdays']),whole(v['orders']['actual_post']),pct(v['orders']['change_percent']),money(v['ordered_value_rub']['expected_same_weekdays']),money(v['ordered_value_rub']['actual_post']),pct(v['ordered_value_rub']['change_percent'])] for k,v in matched.items()])
body+='<p>По этому расчёту стоимость заказов 20 SKU ниже эталона на 6,9%, склеек — на 12,0%, весь аккаунт около прежнего уровня (+0,35%). Это другая база сравнения, а не доказанный эффект ставок. Период также отличается от отчёта о новой волне, которая стартовала 6 ноября.</p>'
body+='<h2>Последние 7 полных дней</h2>'
display=[]
for r in S['latest_equal7_complete_weekdays']:
    display.append([r['from_msk']+' — '+r['through_msk'],money(r['ad']['spend_rub']),r['sales']['own']['orders'],money(r['sales']['own']['ordered_value_RUB']),r['sales']['models']['orders'],money(r['sales']['models']['ordered_value_RUB'])])
body+=table(['Период','Расход, ₽','Заказы 20 SKU','Стоимость 20 SKU, ₽','Заказы склеек','Стоимость склеек, ₽'],display)
latest=S['latest7_weekday_matched28']['own']
body+='<p>Сравнение 04–10.11 с последней неделей до снижения даёт 33 против 31 заказа и +52,7% стоимости. Однако последняя неделя до снижения была слабее 28-дневной базы: по среднемесячному эталону для последних семи дней ожидалось '+money(latest['expected_same_weekdays']['orders'])+' заказа и '+money(latest['expected_same_weekdays']['ordered_value_RUB'])+' ₽. Текущие 33 заказа и 331 375 ₽ ниже этого эталона на '+money(-D(latest['change_percent']['orders']))+'% и '+money(-D(latest['change_percent']['ordered_value_RUB']))+'%. Поэтому уверенно объявлять восстановление продаж пока рано.</p>'
daily=list(csv.DictReader((P/'monitor20_daily.csv').open(encoding='utf-8-sig')))
body+='<h2>По дням после снижения</h2>'+table(['День МСК','Расход, ₽','Общие заказы 20 SKU','Стоимость 20 SKU, ₽','Заказы склеек','Стоимость склеек, ₽'],[[r['date_msk'],money(r['spend_rub']),r['fixed20_orders'],money(r['fixed20_ordered_value_rub']),r['families_union_orders'],money(r['families_union_ordered_value_rub'])] for r in daily])
by_sku=list(csv.DictReader((P/'by_sku_20.csv').open(encoding='utf-8-sig')))
body+='<h2>Все 20 SKU</h2><p>Наибольшее снижение стоимости заказов — артикул 5378: 23 → 13 заказов, 303 473 → 216 886 ₽. Он объясняет 86 587 ₽ из общей разницы 134 678 ₽. Это кандидат для отдельного анализа, не команда менять ставку.</p>'
body+=table(['Артикул','SKU','Расход до, ₽','Расход после, ₽','Заказы до → после','Стоимость до, ₽','Стоимость после, ₽','Δ стоимости, ₽'],[[r['article'],r['sku'],money(r['before_ad_spend_RUB']),money(r['after_ad_spend_RUB']),r['before_own_orders']+' → '+r['after_own_orders'],money(r['before_own_ordered_value_RUB']),money(r['after_own_ordered_value_RUB']),money(r['own_value_change_RUB'])] for r in sorted(by_sku,key=lambda r:D(r['own_value_change_RUB']))])
body+='<h2>Экономическая оценка снижения</h2><p>По наблюдаемым данным снижение выглядит экономически оправданным, но 138 684,90 ₽ — сокращение рекламных расходов, а не автоматически измеренный прирост чистой прибыли. Потерянная стоимость заказов включает закупку и прочие переменные затраты. Для решения важен оставшийся после них маржинальный доход до рекламы.</p>'
actual_pult=P/'margin_review/analysis.json'
economic_path=P/'economics_sensitivity.json'
if actual_pult.exists():
    e=json.loads(actual_pult.read_text())
    r=e['account_average_estimate']
    body+='<p>Уточнение по закрытой октябрьской модели пульта: маржа до CPC после закупки и остальных расходов Ozon — <b>22,55%</b>. Поэтому 169 930 ₽ снижения стоимости заказов склеек соответствуют примерно <b>'+money(r['lost_contribution_RUB'])+' ₽</b> маржинального результата до CPC. С учётом сокращения наблюдаемого CPC-расхода расчётное улучшение — <b>'+money(r['improvement_after_observed_CPC_saving_RUB'])+' ₽ за 11 дней до ФОТ и налогов компании</b>.</p>'
    body+='<p>Это проекция на октябрьских коэффициентах, не финансовый факт ноября. Добавлена обратно только CPC-реклама, остальной промо-раздел оставлен в расходах. Проверка по себестоимости проданных SKU даёт другой ориентир, но имеет неполное покрытие и малые исторические выборки. Фиксированный ФОТ денежной экономией не добавлялся.</p>'
    body+='<p>По 5378 средняя цена единицы в заказах выросла на 31,9%, штуки 24 → 13. При октябрьском себесте 4 017 ₽/шт. и средних удержаниях аккаунта расчётный результат после CPC: −24,4 → +29,6 тыс. ₽. На базу до повлиял ценовой всплеск 22 октября. <a href="/api/wb-bids/reports/ozon-fixed20-margin-2026-11-11">Подробный разбор 5378 и превращения 169,9 тыс. ₽ оборота в результат</a>.</p>'
elif economic_path.exists():
    economics=json.loads(economic_path.read_text())
    body+='<p>Для более широкого сценария берём снижение стоимости заказов всех 19 склеек на 169 930 ₽. Продажи самих 20 SKU уже входят в эту сумму, их второй раз не прибавляем. Ниже сценарии с условной маржой после закупки, комиссий, логистики, упаковки и переменного труда, но до рекламы и постоянных расходов.</p>'
    body+=table(['Условная маржа до рекламы','Утраченная маржа склеек, ₽','Разница: снижение рекламных расходов минус утраченная маржа, ₽'],[[money(r['assumed_pre_ad_contribution_margin_pct'])+'%',money(r['lost_model_group_contribution_RUB']),money(r['illustrative_result_RUB'])] for r in economics['scenarios']])
    body+='<p>Это сценарная оценка по отчётным суммам при прочих равных и сопоставимой налоговой базе, не измеренная чистая прибыль и не доказанный эффект снижения. Фактическая маржа этих продаж не установлена, налоговые корректировки не выполнялись. В этой простой модели без дополнительных эффектов порог маржи до рекламы равен 81,6%.</p>'
    body+='<p>Меньшая нагрузка сотрудников — дополнительный операционный плюс. При фиксированной зарплате свободное время само по себе не уменьшает денежные расходы. Сдельные выплаты и сверхурочные могут сокращаться; если они уже включены в переменные затраты, их нельзя прибавлять к экономии второй раз. <a href="https://www.business.govt.nz/strategy-and-performance/strategic-finance/key-equations-for-your-finances">Определение маржинального дохода и разделение постоянных/переменных расходов</a>.</p>'
body+='<h2>Качество и интерпретация</h2><p>После: 220 из 220 SKU-дней; до: 219 из 220, отсутствует SKU 1510947813 за 29.10, ранее отдельный точечный повтор также не дал строки. Это не доказательство полноты всех 36 размещений. ДРР рассчитан по рекламной стоимости, приписанной Ozon; общие продажи получены независимо из FBS+FBO и считаются по уникальным order_id. 19 склеек — одинаковая зафиксированная карта model_id с обеих сторон, исторические составы не восстановлены.</p>'
body+='<p>Стоимость заказанного — не финансовая выручка/прибыль. Наличие, праздник 4 ноября, новая первая волна с 6 ноября (пересечение пяти моделей), две дополнительные правки ставок 8 ноября и поздняя атрибуция мешают выделить чистый эффект снижения 30 октября. До использована сохранённая база, отмены после снижения перечитаны. Уровни SKU/склеек/аккаунта пересекаются и не складываются.</p>'
slug='/api/wb-bids/reports/ozon-fixed20-2026-11-11'
body+=f'<p><a href="{slug}-periods">Равные 11 дней CSV</a> · <a href="{slug}-daily">Дни CSV</a> · <a href="{slug}-skus">Все 20 SKU CSV</a> · <a href="{slug}-data">Полная сводка JSON</a></p>'
(P/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: 20 SKU после снижения ставок</title><style>body{font:16px/1.55 system-ui;max-width:1450px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}</style>'+body+'</html>')
print('Published offline:20SKU/19models;equal11days;weekday references;no causal/profit claim;no marketplace writes')
