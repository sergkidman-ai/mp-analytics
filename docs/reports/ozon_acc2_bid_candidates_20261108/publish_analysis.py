# поток: mkt / ozon
"""Offline report renderer. No Ozon or production database writes."""
import collections
import csv
import datetime as dt
import html
import json
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path

O = Path(__file__).parent
R = O / 'raw'
read = lambda p: json.loads(Path(p).read_text())
num = lambda x: D(str(x or 0).replace('\xa0', '').replace(' ', '').replace(',', '.'))
money = lambda x: num(x).quantize(D('.01'), rounding=ROUND_HALF_UP)
fmt = lambda x: f'{num(x):,.2f}'.replace(',', ' ').replace('.', ',')
esc = lambda x: html.escape(str(x))


def save(name, value):
    (O / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def csvout(name, rows):
    assert rows
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


summary = read(O / 'analysis_summary_prescreen.json')
assert summary['account'] == 'oz_acc2'
rank = read(O / 'ranked_instock_paid_skus.json')
by_sku = {r['sku']: r for r in rank}
headroom = read(O / 'headroom_check.json')
minima = read(R / 'individual_minima.json')
assert minima['account'] == 'oz_acc2' and minima['http'] == 200
floors = {str(r['sku']): num(r['bid']) for r in minima['response']['minBids']}
assert set(floors) == set(read(O / 'minimum_check_skus.json')['skus'])
campaigns = {str(c['id']): c for c in read(O / 'live_campaigns.json')['response']['list']}
funding = collections.defaultdict(lambda: [D(0), D(0), D(0)])
for p in R.glob('funding28_*.json'):
    x = read(p)
    assert x['account'] == 'oz_acc2' and x['http'] == 200
    assert x['params']['dateFrom'] == '2026-10-11' and x['params']['dateTo'] == '2026-11-07'
    for r in x['response']['rows']:
        for i, k in enumerate(('moneySpent', 'bonusSpent', 'prepaymentSpent')):
            funding[str(r['id'])][i] += num(r[k])
daily_total = D(0)
for p in R.glob('daily28_*.json'):
    x = read(p)
    assert x['account'] == 'oz_acc2' and x['http'] == 200
    for r in x['response']['rows']:
        assert '2026-10-11' <= r['date'] <= '2026-11-07'
        daily_total += num(r['moneySpent'])
assert daily_total == sum(sum(v) for v in funding.values()) == D('307467.75')
csvout('funding_by_campaign.csv', [
    {'account': 'oz_acc2', 'campaign_id': cid, 'name': campaigns[cid]['title'],
     'state': campaigns[cid]['state'], 'payment_type': campaigns[cid]['PaymentType'],
     'money_rub': str(v[0]), 'bonus_rub': str(v[1]), 'prepayment_rub': str(v[2])}
    for cid, v in funding.items()])

with (O / 'raw_ad_placements_28d.csv').open(encoding='utf-8-sig') as f:
    raw = list(csv.DictReader(f))
seen = set()
placement_cost = collections.defaultdict(lambda: D(0))
alias = read(O / 'current_card_map.json')['canonical_by_alias']
for r in raw:
    assert r['account'] == 'oz_acc2'
    key = (r['campaign_id'], r['raw_sku'], r['date_msk'])
    assert key not in seen
    seen.add(key)
    placement_cost[(alias.get(r['raw_sku'], r['raw_sku']), r['campaign_id'])] += num(r['confirmed_paid_CPC_rub'])

zero_room = [r['sku'] for r in headroom['can_lower'] if r['model_orders'] == 0]
assert len(zero_room) == 17
priority = ['1873179620', '1903650705', '2241950954', '1883229813', '1822904362']
secondary = [s for s in zero_room if s not in priority]
conditional = ['1873145260', '1822913370']
selected = priority + secondary + conditional
assert len(selected) == len(set(selected)) == 20
shortlist, plan = [], []
for sku in selected:
    r = by_sku[sku]
    tier = 'FIRST_REVIEW' if sku in priority else 'CONDITIONAL_MODEL_SALES' if sku in conditional else 'ZERO_SALES_EXTENDED_REVIEW'
    if num(r['8d_paid_CPC_rub']) == 0:
        tier = 'WATCH_NO_RECENT_SPEND'
    slots = []
    for p in r['current_placements']:
        old = num(p['bid_rub'])
        new = max(floors[sku], money(old * D('.9'))) if old > floors[sku] else old
        assert new <= old and (new == old or new >= floors[sku])
        slots.append(f"{p['campaign_id']}: {old} → {new}")
        if new == old:
            continue
        assert funding[p['campaign_id']][1] == 0
        plan.append({'account': 'oz_acc2', 'sku': sku, 'article': r['article'],
                     'campaign_id': p['campaign_id'], 'tier': tier,
                     'current_bid_rub': str(old), 'individual_min_rub': str(floors[sku]),
                     'draft_bid_rub': str(new), 'draft_change_percent': str((new / old - 1) * 100),
                     'placement_28d_paid_CPC_rub': str(placement_cost[(sku, p['campaign_id'])]),
                     'status': 'DRAFT_NOT_AUTHORIZED', 'automatic_application_allowed': False,
                     'current_bid_observed_at_utc': p['observed_at_utc'],
                     'min_observed_at_utc': minima['at_utc']})
    shortlist.append({'account': 'oz_acc2', 'sku': sku, 'article': r['article'], 'name': r['name'],
                      'model_id': r['model_id'], 'tier': tier, 'available_units_now': r['available_units'],
                      **{k: v for k, v in r.items() if k.startswith(('28d_', '8d_'))},
                      'individual_min_rub': str(floors[sku]), 'current_to_draft_slots': '; '.join(slots),
                      'status': 'DRAFT_NOT_AUTHORIZED', 'automatic_application_allowed': False})
csvout('candidate_shortlist.csv', shortlist)
csvout('draft_bid_plan.csv', plan)
base_fields = ['sku', 'article', 'name', 'model_id', 'available_units', '28d_paid_CPC_rub',
               '8d_paid_CPC_rub', '28d_SKU_unique_orders', '28d_model_unique_orders',
               '28d_attributed_sales_with_model_rub', '28d_attribution_DRR_percent']
zero_rows = [{**{k: r[k] for k in base_fields}, 'account': 'oz_acc2',
              'current_placements': '; '.join(f"{p['campaign_id']}:{p['bid_rub']}" for p in r['current_placements']),
              'individual_min_checked': r['sku'] in floors,
              'individual_min_rub': str(floors[r['sku']]) if r['sku'] in floors else '',
              'automatic_application_allowed': False}
             for r in rank if r['28d_model_unique_orders'] == 0]
csvout('zero_model_sales_instock.csv', zero_rows)
floor_rows = [{**{k: by_sku[r['sku']][k] for k in base_fields}, 'account': 'oz_acc2',
               'minimum_rub': r['minimum'], 'action': 'PRICE_CUT_UNAVAILABLE_REVIEW_ONLY',
               'automatic_application_allowed': False} for r in headroom['no_lower_headroom']]
csvout('at_floor_review.csv', floor_rows)
assert sum(num(r['28d_paid_CPC_rub']) for r in zero_rows) == num(summary['zero_model_sales_instock_spend'])
assert len(zero_rows) == 949 and len(floor_rows) == 26

first_spend = sum(num(by_sku[s]['28d_paid_CPC_rub']) for s in priority)
first_exposed = sum(num(p['placement_28d_paid_CPC_rub']) for p in plan if p['sku'] in priority)
# Illustrative arithmetic only: historical slot expense multiplied by bid change.
# Actual click price, traffic, orders and funding are not forecast by this calculation.
first_scenario = sum(num(p['placement_28d_paid_CPC_rub']) *
                     (1 - num(p['draft_bid_rub']) / num(p['current_bid_rub']))
                     for p in plan if p['sku'] in priority)
validation = {'account': 'oz_acc2', 'period': summary['period'], 'recent': summary['recent'],
              'raw_duplicate_keys': 0, 'raw_rows': len(raw), 'funding_equals_campaign_report': True,
              'reported_total_rub': str(daily_total), 'cash_CPC_mixed_bonus_unallocated_to_SKU_rub': '109.02',
              'pure_paid_campaign_total_rub': '58986.61', 'SKU_detail_total_rub': '58986.59',
              'rounding_difference_rub': '.02', 'checked_floors': len(floors),
              'at_floor_or_below': len(floor_rows), 'can_lower': len(headroom['can_lower']),
              'zero_model_sales_among_at_floor_checked': sum(r['28d_model_unique_orders'] == 0 for r in floor_rows),
              'shortlist_SKUs': len(shortlist), 'draft_changed_placements': len(plan),
              'first_five_all_placements_expense_rub': str(first_spend),
              'first_five_changed_placements_expense_rub': str(first_exposed),
              'illustrative_first_five_28d_reduction_NOT_FORECAST_rub': str(money(first_scenario)),
              'unmapped_paid_or_sold_models': 0, 'CPO_product_detail_verified': False,
              'marketplace_configuration_mutations': 0, 'production_DB_writes': 0,
              'published_order_identifiers': False, 'made_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
save('validation.json', validation)
save('analysis_result.json', {'summary': summary, 'validation': validation, 'candidates': shortlist})

slug = '/api/wb-bids/reports/ozon-acc2-bid-candidates-2026-11-08'
def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join('<th>'+esc(h)+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+esc(c)+'</td>' for c in row)+'</tr>' for row in rows) + '</tbody></table></div>'

body = '''<h1>Ozon · акк2 · кандидаты на сокращение расходов</h1>
<p>Период: <b>11 октября — 7 ноября 2026, 28 полных дней МСК</b>. Последние 8 дней: 31 октября — 7 ноября.
Состав, ставки, остатки и минимумы проверены API 8 ноября. Настройки рекламы не менялись.</p>
<h2>Что действительно оплачено деньгами</h2>'''
body += table(['Вид расхода', 'Деньги, ₽', 'Комментарий'], [
    ['Оплата за заказ (CPO)', fmt(summary['cash_CPO']), '69,5% денежного расхода; сам размер не доказывает неэффективность'],
    ['Оплата за клик (CPC)', fmt(summary['cash_CPC_all_campaigns']), '58 986,61 в пяти полностью платных РК; 109,02 в смешанных бонусных'],
    ['Всего деньгами', fmt(summary['cash_total']), 'Предоплата 0,00'],
    ['Отдельно бонусами', fmt(summary['bonuses']), 'Не считать потенциальной экономией рублей'],
    ['Всего в рекламной статистике', fmt(daily_total), 'Деньги + бонусы; сумма сверена с expense/json']])
body += f'''<h2>Где виден риск</h2><p>Из 1 214 доступных SKU с платным CPC-расходом у <b>949</b> нет ни одного неотменённого заказа
ни на сам SKU, ни на его текущую склейку за 28 дней. Расход: <b>{fmt(summary['zero_model_sales_instock_spend'])} ₽</b>.
В том числе 128 SKU с расходом от 50 ₽: {fmt(summary['zero_model_sales_instock_spend50'])} ₽.
Это кандидаты на проверку, а не доказанная величина сэкономленных денег: продажи могут прийти позднее.</p>
<p>Индивидуальные минимумы прочитаны для 67 выбранных товаров: у 41 можно снизить хотя бы одну ставку,
у 26 все текущие ставки уже на минимуме или ниже (из них 16 без заказов в склейке).
Для остальных товаров минимумы здесь не проверялись. Наличие проверено у каждого SKU отдельно.</p>
<h2>Первый список для решения</h2><p>Первые пять — самые заметные кандидаты с доступным снижением.
Дальше идут меньшие расходы без заказов. Последние два — условные кандидаты: у их склеек есть продажи.
Заказы ниже — все заказы, рекламные и органические вместе. Склейка включает сам SKU; колонки не складывать.</p>'''
labels = {'FIRST_REVIEW': 'Первый разбор', 'ZERO_SALES_EXTENDED_REVIEW': 'Без заказов, меньший расход',
          'WATCH_NO_RECENT_SPEND': 'Наблюдать: последние 8 дней расход 0', 'CONDITIONAL_MODEL_SALES': 'Условно: склейка продаётся'}
body += table(['Артикул / SKU', 'Расход 28д, ₽', 'Расход 8д, ₽', 'Заказы SKU / склейки 28д',
               'Заказы SKU / склейки 8д', 'ДРР Ozon 28д', 'РК: текущая → проект, ₽', 'Приоритет'],
              [[r['article']+' / '+r['sku'], fmt(r['28d_paid_CPC_rub']), fmt(r['8d_paid_CPC_rub']),
                f"{r['28d_SKU_unique_orders']} / {r['28d_model_unique_orders']}",
                f"{r['8d_SKU_unique_orders']} / {r['8d_model_unique_orders']}",
                fmt(r['28d_attribution_DRR_percent'])+'%' if r['28d_attribution_DRR_percent'] else 'Нет атрибутированной выручки',
                r['current_to_draft_slots'], labels[r['tier']]] for r in shortlist])
body += f'''<p><b>Это только проект.</b> По каждой изменяемой позиции предложено −10% с ограничением индивидуальным минимумом.
По 0898BJH5U125: 11 → 10 ₽ (−9,09%, ниже нельзя). Дешёвые соседние размещения сохраняются.
ДРР = платный CPC / атрибутированная Ozon выручка самого SKU и склейки; это не маржа и не причинная оценка эффекта рекламы.</p>
<p>Пять первых SKU потратили суммарно {fmt(first_spend)} ₽ во всех платных CPC-РК.
Из них {fmt(first_exposed)} ₽ относятся к размещениям, где предлагается снижение.
Если бы расход этих размещений изменился пропорционально ставке, арифметическое сокращение было бы лишь
{fmt(first_scenario)} ₽ за такой же 28-дневный период. <b>Это сценарий, не прогноз</b>: цена клика, число кликов и заказов могут измениться иначе.
На акк2 нет оснований ожидать эффект масштаба прежних дорогих SKU акк1.</p>
<h2>Что не стоит сокращать только из-за большого расхода</h2>
<p>6454AL1ZE8I7: 1 475,07 ₽, 5 собственных / 10 заказов склейки, ДРР 15,78%.
5422YHSC5BIR: 743,61 ₽, 7 / 8 заказов, ДРР 1,29%.
5624H6CW3Y6Y: 503,87 ₽, 9 / 12 заказов, ДРР 0,73%.
6461R2LC52BY: нет рекламно атрибутированной выручки, но у склейки 6 обычных заказов — это не нулевые продажи.</p>
<h2>Ограничения и следующий шаг</h2>
<p>Крупнейший расход — CPO, РК23608084. Товарная детализация не подтверждена: endpoint
all_sku_promo/products/generate отвечает HTTP400 «Некорректный период» на сохранённые запросы.
Нули orders из агрегатного отчёта не используются как доказательство отсутствия заказов.
Следующий разбор для крупной экономии: получить корректную товарную выгрузку CPO и сопоставить её с обычными заказами и экономикой SKU.</p>
<p>Для CPC следующий конкретный шаг — согласовать первый тест снижения пяти SKU, затем проверить первые полные сутки,
SKU, склейку и аккаунт на одинаковых днях недели. Товары на минимуме требуют отдельного решения о тестовом выключении;
не удалять и не выключать автоматически. Сейчас никакие ставки, бюджеты, составы и состояния кампаний не изменены.</p>
<p>Склейки — текущие Ozon model_id, а не семьи по первым четырём цифрам артикула. Исторический состав склеек не восстановлен.
Остатки текущие, наличие на каждый исторический день неизвестно. Заказы собраны FBS+FBO за весь период,
отменённые отправления исключены; 348 уникальных заказов на 3 302 105 ₽ заказанной стоимости.
Это не реализованная финансовая выручка и не прибыль. Один заказ может входить в разные SKU/склейки; сумму строк не считать заказами аккаунта.</p>
<p>Источники: Performance campaign list/current products, statistics/daily/json, statistics/expense/json,
два готовых исторических SKU-отчёта (11 РК), Seller product/info (3 403 точных SKU), FBS/FBO postings, min/sku (67 SKU).
Аккаунт всех источников oz_acc2; данные oz_acc1 не использовались. Смешанные 109,02 ₽ не распределены пропорционально на SKU.
Расхождение чисто платных SKU-строк и суммы РК 0,02 ₽ сохранено как округление.</p>'''
body += '<h2>Скачать</h2><ul>' + ''.join(f'<li><a href="{slug}-{suffix}">{title}</a></li>' for suffix, title in [
    ('skus', '20 кандидатов с обоими периодами и склейками · CSV'), ('plan', 'Проект ставок по точным размещениям · CSV'),
    ('zero', 'Все 949 доступных SKU без заказов в склейке · CSV'), ('floor', '26 проверенных SKU без возможности снизить ставку · CSV'),
    ('funding', 'Деньги и бонусы по кампаниям · CSV')]) + '</ul>'
(O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon акк2 — расходы</title><style>body{font:16px/1.55 system-ui,sans-serif;max-width:1400px;margin:30px auto;padding:0 18px;color:#172239;background:#f6f8fc}h1{font-size:28px}h2{margin-top:30px}table{border-collapse:collapse;width:100%;background:white;font-size:14px}td,th{padding:10px;border:1px solid #d8deea;text-align:left;vertical-align:top}th{background:#e8eef8}.scroll{overflow-x:auto}a{color:#155ab6}p{max-width:1150px}</style>'+body+'</html>')
(O / 'RESULT.md').write_text(f'''# oz_acc2: проверка расходов, 08.11.2026

11.10–07.11, 28 полных дней МСК. Только чтение Ozon.
Деньги: CPC 59 095,63 ₽; CPO 134 791,85 ₽; всего 193 887,48 ₽.
Бонусы отдельно: 113 580,27 ₽. Сумма с бонусами 307 467,75 ₽ сверена.
949 доступных SKU без обычных заказов у SKU/текущей склейки: 27 711,19 ₽ CPC;
из них 128 с расходом ≥50 ₽: 14 217,45 ₽. Это риск, не гарантированная экономия.
Проверено 67 минимумов: 41 с запасом в одном из размещений, 26 без запаса.
Первый список: 0898BJH5U125, 5791XEEPJPBD, 4368I15TU58O, 3075AUSU2S08, 0194TFN23ZUX.
Их расход {fmt(first_spend)} ₽; арифметический сценарий предложенного снижения {fmt(first_scenario)} ₽/28д, НЕ прогноз.
20 кандидатов, 17 из них вообще без заказов и с запасом ставки; проект не применён.
CPO SKU-детализация отсутствует: сохранённые GET вернули 400 «Некорректный период».
Крупнейший следующий разбор — корректная товарная выгрузка CPO; для CPC — решение о тесте первых пяти.
Данные: raw/, sku_reports/, private_general_sales_lines.csv (не публиковать), current_card_map.json.
Готовые таблицы и validation.json рядом; index.html опубликован через закрытый BI.
https://bi.beyindi.com{slug}
''')
print(json.dumps(validation, ensure_ascii=False))
