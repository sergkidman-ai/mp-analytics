# поток: mkt / ozon
"""Offline analysis of saved fixed20 observations and closed Pult finance. No API/DB writes."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

P = Path(__file__).resolve().parent
REPO = next(p for p in P.parents if (p / 'CLAUDE.md').exists())
sys.path.insert(0, str(REPO))
from core.ozon_posting import product_price

O = P.parent
BASE = REPO / 'docs/reports/ozon_launch_20261106/baseline'
SKU = '1510947813'
S = json.loads((O / 'status.json').read_text())
UNIT = json.loads((P / 'cohort_unit_model_inputs.json').read_text())
HIST = REPO / 'reports/data/mp_ozon_hist.json'
hist = json.loads(HIST.read_text())
i = hist['period_keys'].index('2026-10')
account = hist['accounts']['oz_acc1']
lines = {k: D(str(v[i])) for k, v in account['lines'].items()}
cpc = D(str(account['detail']['promo']['OP:OperationMarketplaceCostPerClick'][i]))
cogs = D(str(account['cogs'][i]))
net = D(str(account['net'][i]))
expenses = sum(lines[k] for k in ('commission', 'delivery', 'partners', 'fbo', 'promo', 'penalty', 'unclassified'))
payout = lines['sales'] - expenses - lines['returns'] + lines['compensation'] + lines['other']
assert payout - cogs == net
margin_before_cpc = (net + cpc) / lines['sales']
keep_before_cogs_cpc = (payout + cpc) / lines['sales']
assert abs(keep_before_cogs_cpc - cogs / lines['sales'] - margin_before_cpc) < D('1e-24')
assert lines['promo'] >= cpc > 0


def save_json(name, value):
    (P / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


sources = [HIST, O / 'status.json', O / 'by_sku_20.csv', O / 'merged_ads.csv',
           P / 'expense_basis_status.json', P / '5378_margin_rows.json', P / '5378_dashboard_oct_qty.json',
           P / 'cohort_october_financial_rows.json', P / 'cohort_unit_model_inputs.json',
           BASE / 'sales_lines.csv', REPO / 'reports/ozon_margin_control.py']
source_hashes = {str(p.relative_to(REPO)): sha256(p.read_bytes()).hexdigest() for p in sources}
save_json('pult_closed_october_reference.json', {
    'account': 'oz_acc1', 'period': '2026-10', 'source': str(HIST.relative_to(REPO)),
    'lines': lines, 'cogs': cogs, 'net_after_all_MP_costs_and_cogs': net,
    'CPC_only_addback': cpc, 'payout_after_MP_before_cogs': payout,
    'margin_before_CPC': margin_before_cpc, 'keep_before_cogs_and_CPC': keep_before_cogs_cpc,
    'source_sha256': source_hashes[str(HIST.relative_to(REPO))],
    'note': 'Аналитическая оценка, не изменение действующего правила пола/удержаний. ФОТ/налоги компании не включены.'})

before, after = S['periods']
ads_saved = list(csv.DictReader((O / 'merged_ads.csv').open(encoding='utf-8-sig')))
own_saved = next(r for r in csv.DictReader((O / 'by_sku_20.csv').open(encoding='utf-8-sig')) if r['sku'] == SKU)
sku_input = next(r for r in UNIT if r['sku'] == SKU)
assert D(sku_input['unit_cogs']) > 0

period_stats = {}
daily_rows = []
placements = []
for period in (before, after):
    phase, lo, hi = (period[k] for k in ('phase', 'from_msk', 'through_msk'))
    by_day = collections.defaultdict(lambda: {'orders': set(), 'qty': D(0), 'value': D(0), 'prices': []})
    files = [BASE / f'postings_{scheme}.json' if phase == 'before'
             else O / 'raw' / f'monitor_postings_20261031_20261110_{scheme}.json'
             for scheme in ('fbs', 'fbo')]
    seen = set()
    for source in files:
        source_hashes[str(source.relative_to(REPO))] = sha256(source.read_bytes()).hexdigest()
        raw = json.loads(source.read_text())
        assert raw['complete']
        for posting in raw['postings']:
            if posting.get('status') == 'cancelled':
                continue
            day = dt.datetime.fromisoformat(posting['in_process_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
            if not lo <= day <= hi:
                continue
            for product in posting.get('products', []):
                if str(product.get('sku')) != SKU:
                    continue
                key = (posting['posting_number'], SKU)
                assert key not in seen
                seen.add(key)
                q, price = D(str(product['quantity'])), product_price(product)
                r = by_day[day]
                r['orders'].add(str(posting['order_id']))
                r['qty'] += q
                r['value'] += q * price
                r['prices'].append(price)
    qty = sum((r['qty'] for r in by_day.values()), D(0))
    value = sum((r['value'] for r in by_day.values()), D(0))
    orders = len(set().union(*(r['orders'] for r in by_day.values())))
    assert value == D(sku_input[phase + '_value']) == D(own_saved[phase + '_own_ordered_value_RUB'])
    assert qty == D(sku_input[phase + '_qty'])
    assert orders == int(own_saved[phase + '_own_orders'])
    selected = [r for r in ads_saved if r['sku'] == SKU and lo <= r['date_msk'] <= hi]
    spend = sum((D(r['spend_rub']) for r in selected), D(0))
    ad_value = sum((D(r['ad_revenue_rub']) for r in selected), D(0))
    assert spend == D(own_saved[phase + '_ad_spend_RUB'])
    est_cogs = qty * D(sku_input['unit_cogs'])
    pre_cpc_result = value * keep_before_cogs_cpc - est_cogs
    period_stats[phase] = {
        'from_msk': lo, 'through_msk': hi, 'own_orders': orders, 'units': qty,
        'ordered_value_RUB': value, 'average_ordered_price_per_unit_RUB': value / qty,
        'historical_oct_unit_COGS_RUB': D(sku_input['unit_cogs']),
        'observed_CPC_spend_RUB': spend, 'ad_attributed_value_RUB': ad_value,
        'ad_DRR_percent': spend / ad_value * 100,
        'estimated_COGS_RUB': est_cogs, 'estimated_result_before_CPC_RUB': pre_cpc_result,
        'estimated_result_after_CPC_RUB': pre_cpc_result - spend,
    }
    day = dt.date.fromisoformat(lo)
    while day.isoformat() <= hi:
        date = day.isoformat()
        r = by_day[date]
        day_ads = [v for v in selected if v['date_msk'] == date]
        daily_rows.append({'phase': phase, 'date_msk': date, 'orders': len(r['orders']),
                           'units': r['qty'], 'ordered_value_RUB': r['value'],
                           'average_price_RUB': r['value'] / r['qty'] if r['qty'] else None,
                           'min_price_RUB': min(r['prices']) if r['prices'] else None,
                           'max_price_RUB': max(r['prices']) if r['prices'] else None,
                           'observed_ad_spend_RUB': sum((D(v['spend_rub']) for v in day_ads), D(0)) if day_ads else None})
        day += dt.timedelta(days=1)
    for campaign in sorted({r['campaign_id'] for r in selected}):
        rr = [r for r in selected if r['campaign_id'] == campaign]
        placements.append({'phase': phase, 'campaign_id': campaign, 'observed_days': len({r['date_msk'] for r in rr}),
                           'spend_RUB': sum((D(r['spend_rub']) for r in rr), D(0)),
                           'views': sum(int(D(r['views'])) for r in rr), 'clicks': sum(int(D(r['clicks'])) for r in rr)})

ad_saving = D(before['ad']['spend_rub']) - D(after['ad']['spend_rub'])
model_value_loss = D(before['sales']['models']['ordered_value_RUB']) - D(after['sales']['models']['ordered_value_RUB'])
account_average_loss = model_value_loss * margin_before_cpc
covered = {}
for period in (before, after):
    phase = period['phase']
    assert sum(D(r[phase + '_value']) for r in UNIT) == D(period['sales']['models']['ordered_value_RUB'])
    known = [r for r in UNIT if r['unit_cogs'] is not None and D(r['unit_cogs']) > 0]
    known_value = sum((D(r[phase + '_value']) for r in known), D(0))
    known_cogs = sum((D(r[phase + '_qty']) * D(r['unit_cogs']) for r in known), D(0))
    covered[phase] = {'covered_ordered_value_RUB': known_value, 'estimated_COGS_RUB': known_cogs,
                       'covered_turnover_percent': known_value / D(period['sales']['models']['ordered_value_RUB']) * 100,
                       'estimated_contribution_before_target_CPC_RUB': known_value * keep_before_cogs_cpc - known_cogs,
                       'uncovered_ordered_value_RUB': D(period['sales']['models']['ordered_value_RUB']) - known_value}
covered_loss = covered['before']['estimated_contribution_before_target_CPC_RUB'] - covered['after']['estimated_contribution_before_target_CPC_RUB']
missing = [r for r in UNIT if r['unit_cogs'] is None or D(r['unit_cogs']) <= 0]
result = {
    'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'account': 'oz_acc1', 'article': '5378', 'sku': SKU,
    'model': 'Октябрьские затраты пульта, проекция на стоимость заказанного; до ФОТ/налогов компании; не финансовый факт ноября и не причинный эффект ставок.',
    'scope': '19 native model_id, включают 20 SKU. Уровни не складывать.',
    'closed_reference_month': '2026-10', 'CPC_addback_only_RUB': cpc,
    'average_account_contribution_before_CPC_percent': margin_before_cpc * 100,
    'model_ordered_value_loss_RUB': model_value_loss, 'observed_target_CPC_saving_RUB': ad_saving,
    'account_average_estimate': {'lost_contribution_RUB': account_average_loss,
                                 'improvement_after_observed_CPC_saving_RUB': ad_saving - account_average_loss},
    'unit_COGS_check': {'covered': covered, 'covered_SKUs': len(known), 'sold_SKUs': len(UNIT),
                       'unknown_COGS_SKUs': missing, 'covered_contribution_loss_RUB': covered_loss,
                       'covered_result_change_including_all_target_CPC_saving_RUB': ad_saving - covered_loss,
                       'thin_oct_reference_SKUs_qty_lt5': [r['sku'] for r in known if D(r['historical_qty']) < 5],
                       'note': 'Чувствительность к товарному миксу, не независимый факт. Неизвестная себестоимость не ноль; результат шести SKU не оценён. Удержания по SKU заменены средней долей аккаунта; часть исторических qty мала.'},
    '5378': period_stats, '5378_placements': placements,
    'data_limits': ['До нет рекламной строки 5378 за 29.10; не ноль.',
                    'История всех 36 размещений не доказана; saving по наблюдаемым строкам.',
                    'Праздник, разный набор дней недели, новая волна с 06.11 и цена/скидки мешают причинному выводу.',
                    'Коэффициент MP включает средние возвраты, CPO, подписки и прочие расходы. Только CPC добавлен обратно.',
                    'Объём заказанного отличается от закрытой реализации; ноябрьские удержания/возвраты могут измениться.',
                    'Фиксированный ФОТ не экономится автоматически; труд/налоги компании в этой модели не рассчитаны.'],
    'source_sha256': source_hashes,
}
save_json('analysis.json', result)
with (P / '5378_daily.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=daily_rows[0].keys()); w.writeheader(); w.writerows(daily_rows)

money = lambda n: f'{D(str(n)):,.0f}'.replace(',', ' ') if n is not None else 'нет данных'
def table(head, rows):
    return '<div class="scroll"><table><tr>' + ''.join('<th>' + escape(str(h)) + '</th>' for h in head) + '</tr>' + ''.join('<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in r) + '</tr>' for r in rows) + '</table></div>'

body = '<h1>5378 и экономическая оценка снижения ставок 20 SKU</h1>'
body += '<p><b>По средней модели пульта 169 930 ₽ снижения стоимости заказов склеек соответствуют примерно 38 323 ₽ маржинального результата до CPC. При сокращении наблюдаемого CPC-расхода на 138 685 ₽ ориентир улучшения — 100 362 ₽ за 11 дней до ФОТ и налогов компании.</b> Это расчёт на октябрьских коэффициентах, а не установленная прибыль ноября.</p>'
body += '<p>Аккаунт oz_acc1. До: 19–29.10.2026; после: 31.10–10.11.2026, по 11 полных дней МСК. День снижения 30.10 и неполный 11.11 исключены. Ставки/РК не менялись.</p>'
body += '<h2>Что настроено в пульте</h2><p>Взята закрытая октябрьская модель «Отчёты МП» и подтверждённая база расходов Ozon. Себестоимость — готовая финансовая витрина отгрузок МС; она не пересобиралась.</p>'
body += table(['Октябрь, весь oz_acc1', '₽'], [['Продажи', money(lines['sales'])], ['Расходы услуг и комиссий Ozon, включая рекламу', money(expenses)], ['Возвраты продаж', money(lines['returns'])], ['Компенсации и прочие доходы', money(lines['compensation'] + lines['other'])], ['Себестоимость', money(cogs)], ['Результат пульта после затрат МП и закупки, до ФОТ/налогов компании', money(net)], ['Добавляем обратно только CPC, чтобы не считать рекламу дважды', money(cpc)], ['Результат до CPC', money(net + cpc)]])
body += '<p>Маржа после расходов МП и закупки: 17,75%. До CPC: (2 974 276 + 805 015) / 16 758 107 = <b>22,55%</b>. CPO, отзывы, подписки и остальные статьи оставлены расходами. Весь рекламный/промо-раздел обратно не добавлялся. Расчёт не меняет действующий пол отгрузки и цен.</p>'
body += '<h2>Во что превращаются 169,9 тыс. ₽</h2>'
body += table(['Расчёт', 'Снижение маржинального результата до CPC, ₽', 'Сокращение CPC минус снижение результата, ₽'], [['Средняя маржа аккаунта 22,55% × 169 930 ₽', money(account_average_loss), money(ad_saving - account_average_loss)], ['Проверка по индивидуальной октябрьской себестоимости проданных SKU', money(covered_loss), money(ad_saving - covered_loss)]])
body += '<p>Основной ориентир — первая строка. Вторая учитывает цену, количество и микс товаров: 29 из 35 проданных SKU, покрытие оборота до 99,75%, после 98,61%. У шести SKU нет подтверждённого октябрьского себеста: 1 794 ₽ стоимости заказов до и 7 691 ₽ после. Их результат не подставлен нулём. У 17 из 29 стоимостных опор историческая выборка менее пяти единиц, а удержания взяты средними по аккаунту. Поэтому вторая строка — проверка чувствительности, не точная прибыль и не доверительный интервал.</p>'
body += '<p>20 SKU входят в эти 19 склеек; потери SKU и склеек не суммировались. Экономия касается CPC целевых SKU по полученным отчётным строкам, а не изменения всего рекламного бюджета аккаунта.</p>'
body += '<h2>Артикул 5378 · SKU 1510947813</h2><p>Картриджи 069H для Canon. Общие заказы товара отделены от рекламной атрибуции; обе наблюдаемые кампании 10659516 и 12704286 суммированы.</p>'
b, a = period_stats['before'], period_stats['after']
body += table(['Показатель', '19–29 октября', '31 октября–10 ноября'], [['Общие заказы', b['own_orders'], a['own_orders']], ['Штук', money(b['units']), money(a['units'])], ['Стоимость заказов, ₽', money(b['ordered_value_RUB']), money(a['ordered_value_RUB'])], ['Средняя цена единицы в заказах, ₽', money(b['average_ordered_price_per_unit_RUB']), money(a['average_ordered_price_per_unit_RUB'])], ['Расход CPC по полученным строкам, ₽', money(b['observed_CPC_spend_RUB']), money(a['observed_CPC_spend_RUB'])], ['ДРР по рекламной атрибуции Ozon', f"{b['ad_DRR_percent']:.2f}%", f"{a['ad_DRR_percent']:.2f}%"], ['Расчётный результат после CPC, до ФОТ/налогов, ₽', money(b['estimated_result_after_CPC_RUB']), money(a['estimated_result_after_CPC_RUB'])]])
body += '<p>Сокращение наблюдаемого расхода — <b>46 634 ₽</b>. Средняя цена единицы выросла на 31,9%; количество упало на 11 штук. 22 октября было 9 заказов / 10 штук / 93 634 ₽: средняя цена всего 9 363 ₽. Это существенный ценовой/скидочный всплеск в базе до снижения. После — 13 штук по 16 042–17 019 ₽. Поэтому падение количества нельзя целиком приписать ставке.</p>'
body += '<p>Для оценки 5378 использован тот же способ себестоимости на единицу, что в пульте: 192 824,97 ₽ октябрьского себеста / 48 доставленных единиц = <b>4 017,19 ₽/шт.</b> Средняя доля, остающаяся после всех затрат МП, кроме CPC и закупки, — 42,5037% стоимости. Формула: стоимость заказов × 42,5037% − штуки × 4 017,19 ₽ − наблюдаемый CPC. Получается −24 394 → +29 627 ₽, изменение +54 021 ₽. Это модель на октябрьской себестоимости и средней доле удержаний, не финансовый факт SKU за эти дни.</p>'
body += '<h2>5378 по дням: цена, объём, расход</h2>'
body += table(['Период', 'День МСК', 'Заказы', 'Штук', 'Стоимость заказов, ₽', 'Цена/шт. средняя, ₽', 'Наблюдаемый CPC, ₽'], [[r['phase'], r['date_msk'], r['orders'], money(r['units']), money(r['ordered_value_RUB']), money(r['average_price_RUB']), money(r['observed_ad_spend_RUB'])] for r in daily_rows])
body += '<h2>Ограничения и решение</h2><p>По 5378 за 29.10 рекламной строки нет: это неизвестный расход, не ноль. Полнота всех 36 известных размещений 20 SKU за историю не доказана. Стоимость заказанного отличается от финансовой реализации; ноябрьские возвраты и удержания ещё могут изменить результат. Праздник 4.11, состав дней недели и новая волна с 6.11 также влияют на сравнение. ФОТ, налоги и высвободившееся время сотрудников денежной экономией не добавлялись.</p>'
body += '<p><b>Снижение ставок по этим данным выглядит оправданным. Для 5378 оснований восстанавливать дорогие клики ради прежнего числа заказов пока нет.</b> Следующий шаг — сверить ноябрьские фактические удержания и себестоимость после доставки заказов; до этого продолжать наблюдать цену, количество и результат при действующих ставках.</p>'
slug = '/api/wb-bids/reports/ozon-fixed20-margin-2026-11-11'
body += f'<p><a href="{slug}-data">Расчёт и методика JSON</a> · <a href="{slug}-daily">5378 по дням CSV</a> · <a href="/api/wb-bids/reports/ozon-fixed20-2026-11-11">Основной отчёт 20 SKU</a></p>'
(P / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: 5378 и маржа 20 SKU</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}</style>' + body + '</html>')
print(json.dumps({'Pult_pre_CPC_margin_pct': str(margin_before_cpc * 100), 'lost_contribution_RUB': str(account_average_loss), 'estimated_improvement_RUB': str(ad_saving - account_average_loss), '5378_modeled_change_RUB': str(a['estimated_result_after_CPC_RUB'] - b['estimated_result_after_CPC_RUB']), 'covered_COGS_check_RUB': str(ad_saving - covered_loss)}, ensure_ascii=False))
