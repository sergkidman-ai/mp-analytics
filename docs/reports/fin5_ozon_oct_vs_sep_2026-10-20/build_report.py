# поток: fin
"""Новый анализ актуального среза Пульта: октябрь против сентября, два Ozon.

Строится из сохранённых снимков HTTP/финансов/витрины и официальных expense CSV.
Без изменений БД, Пульта или площадок. Все суммы прибыли — до ОПЕКС и налогов.
"""
import csv
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

from reports import ozon_mp_report as O

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
PREVIOUS = OUT.parent / 'fin5_oct_2026-10-19'
NAMES = {'oz_acc1': 'Цифровой', 'oz_acc2': 'Дисквер'}
LABELS = {'sales': 'Оборот', 'itog': 'Удержания МП без возвратов', 'payout': 'К перечислению',
          'cogs': 'Себестоимость', 'net': 'Прибыль до ОПЕКС и налогов', 'margin': 'Маржа',
          'commission': 'Комиссия', 'delivery': 'Доставка', 'partners': 'Услуги партнёров',
          'fbo': 'Складские услуги', 'promo': 'Продвижение, отзывы и подписка',
          'penalty': 'Штрафы и прочие удержания', 'unclassified': 'Неклассифицировано',
          'returns': 'Возвраты', 'compensation': 'Компенсации', 'other': 'Прочие начисления'}


def read(name, directory=OUT):
    return json.loads((directory / name).read_text())


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def rub(value):
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',')


def signed(value):
    return ('+' if value > 0 else '−' if value < 0 else '') + rub(abs(value))


def table(headers, rows):
    return '<div class="scroll"><table><thead><tr>' + ''.join(
        '<th>' + html.escape(str(x)) + '</th>' for x in headers) + \
        '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(
            '<td>' + html.escape(str(x)) + '</td>' for x in row) + '</tr>'
            for row in rows) + '</tbody></table></div>'


def main():
    protected = ['reports/data/mp_ozon_hist.json', 'reports/ozon_mp_report.py',
                 'reports/ozon_mp_page.py', 'web/static/reports.html']
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in protected}
    monthly = read('monthly.json')
    api = read('pult_current.json')
    coverage = read('cogs_coverage.json')
    previous = read('monthly_subscription_reclassified.json', PREVIOUS)
    matched = read('matched_17_days.json')
    funding = [row for account in read('ad_funding_17_days.json') for row in account]
    capture = dt.datetime.fromtimestamp((OUT / 'pult_current.json').stat().st_mtime, dt.timezone.utc)
    changes, components, promo_parts, checks = [], [], [], []
    snapshot = {a: {m: next(x for x in monthly if x['account'] == a and x['month'] == m)
                    for m in ('2026-09', '2026-10')} for a in NAMES}
    for account in NAMES:
        current, september = snapshot[account]['2026-10'], snapshot[account]['2026-09']
        now, prev = current['values'], september['values']
        old = next(x['values'] for x in previous if x['account'] == account and x['month'] == '2026-10')
        for key in ('sales', 'itog', 'payout', 'cogs', 'net', 'margin', 'promo'):
            assert O._fmt(key, now[key], now['sales']) == api['accounts'][account][key]['jul']['txt'], (account, key)
        for line in O.DETAIL_LINES:
            assert abs(sum(current['detail'][line].values()) - now[line]) < 0.005
        parts = []
        for key in O.EXP_SVC + ['returns', 'cogs', 'compensation', 'other']:
            cur_pct, sep_pct = 100 * now[key] / now['sales'], 100 * prev[key] / prev['sales']
            sign = 1 if key in ('compensation', 'other') else -1
            parts.append(dict(account=account, key=key, label=LABELS[key], now=now[key],
                september=prev[key], now_pct=cur_pct, september_pct=sep_pct,
                margin_impact_pp=sign * (cur_pct - sep_pct)))
        assert abs(sum(x['margin_impact_pp'] for x in parts) - (now['margin'] - prev['margin'])) < 1e-8
        components.extend(parts)
        for code in sorted(set(current['detail']['promo']) | set(september['detail']['promo'])):
            a, b = current['detail']['promo'].get(code, 0), september['detail']['promo'].get(code, 0)
            promo_parts.append(dict(account=account, code=code, label=O.det_label(code),
                now=a, september=b, now_pct=100*a/now['sales'], september_pct=100*b/prev['sales'],
                margin_impact_pp=100*(b/prev['sales']-a/now['sales'])))
        promo_parent = next(x['margin_impact_pp'] for x in parts if x['key'] == 'promo')
        assert abs(sum(x['margin_impact_pp'] for x in promo_parts if x['account'] == account) - promo_parent) < 1e-8
        cov = next(x for x in coverage if x['account'] == account and x['period_from'] == '2026-10-01')
        changes.append(dict(account=account, since_previous_analysis={key: now[key]-old[key]
            for key in ('sales','itog','payout','returns','cogs','net','margin')},
            finance_last_date=current['last_date'], cogs_computed_at=cov['last_computed'],
            cogs_sku_revenue=float(cov['sku_revenue']),
            finance_revenue_minus_cogs_sku_revenue=now['sales']-float(cov['sku_revenue']),
            payout_share_september=100*prev['payout']/prev['sales'],
            payout_share_october=100*now['payout']/now['sales']))
        for month in ('2026-09', '2026-10'):
            finance = next(x for x in matched if x['account'] == account and x['month'] == month)
            expense = next(x for x in funding if x['account'] == account and x['month'] == month)
            assert abs(finance['cash_cpc_cpo']-float(expense['cash'])) < 0.005
            checks.append(dict(account=account, month=month, cash_expense_equals_finance=True))
    save('comparison.json', components)
    save('promotion_components.json', promo_parts)
    save('since_previous_analysis.json', changes)
    save('verification.json', dict(pult_cells_match=True, margin_decompositions_reconcile=True,
        promotion_details_reconcile=True, cash_funding_checks=checks,
        protected_sha256=hashes, production_changed=False, snapshot_utc=capture.isoformat()))

    parts = ['''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Ozon: октябрь против сентября — актуальный Пульт</title><style>body{font:16px/1.6 system-ui;color:#182638;background:#f7f9fc;margin:0}main{max-width:1200px;margin:auto;padding:24px}section{background:white;border:1px solid #dce2ea;border-radius:12px;padding:20px;margin:18px 0}h1{font-size:29px;line-height:1.2}h2{font-size:22px}h3{font-size:18px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:right;border-bottom:1px solid #dce2ea;padding:9px;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{background:#edf2f7}.warn{padding:16px;background:#fff1d6;border-left:5px solid #d99a20}.note{color:#536577}a{color:#1259a8}code{overflow-wrap:anywhere}@media(max-width:600px){main{padding:12px}section{padding:13px}h1{font-size:24px}}</style><main><h1>Ozon: октябрь к сентябрю — Цифровой и Дисквер</h1>''']
    parts.append(f'<p>Новый снимок работающего Пульта: {capture:%d.%m.%Y %H:%M} UTC. Сентябрь — закрытый отчёт Пульта; октябрь — факт на текущие даты, без прогноза. Прибыль до ОПЕКС и налогов.</p>')
    parts.append('''<p class="warn"><b>Главное изменение — у Цифрового.</b> Пульт показывает20,31% вместо прежних17,05%, но себестоимость осталась1 874 088,03 ₽. Финансовый оборот уже10 527 949 ₽, а выручка витрины себестоимости ещё9 201 402 ₽. Разница1 326 547 ₽ — оборот, а не сумма недостающей себестоимости. Показанную маржу20,31% пока нельзя считать подтверждённой итоговой рентабельностью.</p><section><h2>1. Актуальные показатели Пульта</h2>''')
    rows = []
    for account in NAMES:
        for month in ('2026-09','2026-10'):
            entry=snapshot[account][month];v=entry['values']
            period='Сентябрь полностью' if month=='2026-09' else 'Октябрь по '+entry['last_date'][8:10]+'.10'
            rows.append([NAMES[account],period,rub(v['sales']),rub(v['itog']),rub(v['cogs']),rub(v['net']),rub(v['margin'])+'%'])
    parts.append(table(['Кабинет','Период','Оборот, ₽','Удержания МП, ₽','COGS, ₽','Прибыль, ₽','Маржа'],rows))
    parts.append('<p class="note">У Цифрового финансовые строки по20.10, денежные CPC/CPO по19.10, себестоимость рассчитана19.10 в02:24UTC. У Дисквера финансовые строки по18.10, CPC/CPO по17.10, COGS рассчитана18.10 в19:40UTC. Последняя дата не означает, что день полностью закрыт.</p></section>')
    parts.append('<section><h2>2. Что изменилось относительно прежнего анализа</h2>')
    parts.append(table(['Кабинет','Добавилось оборота, ₽','Изменение удержаний, ₽','Изменение COGS, ₽','Изменение прибыли, ₽','Маржа, изменение п.п.'],[
        [NAMES[x['account']],signed(x['since_previous_analysis']['sales']),signed(x['since_previous_analysis']['itog']),
         signed(x['since_previous_analysis']['cogs']),signed(x['since_previous_analysis']['net']),signed(x['since_previous_analysis']['margin'])] for x in changes]))
    parts.append('<p>Дисквер остался на прежнем финансовом срезе. У Цифрового появились новые продажи и удержания, но COGS ещё не обновлена. Из улучшения показанной маржи к сентябрю на2,48п.п. около2,13п.п. даёт снижение доли COGS. Доля суммы к перечислению до себестоимости изменилась значительно меньше:37,76→38,11% (+0,35п.п.).</p></section>')
    parts.append('<section><h2>3. Цифровой: денежные расходы всё ещё давят на результат</h2><p>Удержания МП59,67→60,40% оборота: +0,73п.п. Доля комиссии44,14→44,87% (+0,73п.п.), продвижения9,90→10,25% (+0,35п.п.), доставки2,83→2,95%, услуг партнёров1,61→1,75%. Улучшились возвраты2,99→1,92% и штрафы1,17→0,68%. Снижение доли COGS19,93→17,80% пока содержит отставание витрины, поэтому не трактуем его как доказанную экономию на закупке.</p><p>Денежные CPC472 278,97 ₽ и оплата за заказ509 283,45 ₽, вместе981 562,42 ₽. На одинаковых1–17 денежная реклама выросла724 411,22→892 873,74 ₽ (+23,26%) при росте оборота9,87%. Её доля выросла9,18→10,30%. Это подтверждённый сигнал: проверить отдачу денежных кликов и продвижения за заказ.</p></section>')
    parts.append('<section><h2>4. Дисквер: возвраты, первые отзывы и полная подписка</h2><p>Маржа16,66→14,96% (−1,70п.п.). При этом удержания за услуги и комиссию снизились60,83→56,52% оборота. Главный отрицательный сдвиг — возвраты:0,93→8,92%, начислено168 984 ₽. Их арифметический вклад−8,00п.п. частично компенсирован возвратом комиссии и изменением COGS; это не отдельная сумма чистого убытка.</p><p>Два крупнейших сторно продаж: заказ44671170-0530 —93 342 ₽ с возвратом комиссии43 870,74 ₽; отправление0200788250-0167-2 —18 333 ₽ с возвратом комиссии8 616,51 ₽. По первому возврат подтверждён данными ClientReturn; чистые связанные удержания Ozon в прежней сверке5 125,39 ₽ до COGS. <a href="../fin5_oct_2026-10-19/return_44671170.html">Подробности подтверждённого возврата</a>.</p><p>Первые отзывы36 966,14 ₽ против19 194 ₽ за весь сентябрь: доля0,68→1,95%, давление−1,27п.п. Полная подписка34 990 ₽ против24 990 ₽: доля0,89→1,85%, давление−0,96п.п. Общая строка продвижения8,89→9,31% оборота (−0,42п.п. маржи), несмотря на снижение доли CPC/CPO.</p><p>На одинаковых1–17 денежные CPC/CPO88 391,78→94 297,19 ₽ (+6,68%), оборот+71,90%, доля рекламы8,16→5,06%. Бонусами оплачено139 030,05 ₽, отдельно от денежных94 297,19 ₽. Версия о резком росте денежных рекламных расходов не подтверждается.</p></section>')
    parts.append('<section><h2>5. Точное разложение изменения маржи</h2><p>Положительный вклад помогает марже, отрицательный ухудшает. Сумма вкладов равна изменению маржи. Октябрь неполный, сентябрь полный; фикс-плата уже начислена целиком. Вклады включают структуру продаж и даты начислений, не только тарифы.</p>')
    for account in NAMES:
        parts.append('<h3>'+NAMES[account]+'</h3>')
        parts.append(table(['Статья','Сентябрь, % оборота','Октябрь, % оборота','Вклад в маржу, п.п.'],[
            [x['label'],rub(x['september_pct']),rub(x['now_pct']),signed(x['margin_impact_pp'])]
            for x in sorted((x for x in components if x['account']==account),key=lambda x:x['margin_impact_pp'])]))
    parts.append('</section><section><h2>6. Денежное продвижение: состав по статьям</h2>')
    for account in NAMES:
        parts.append('<h3>'+NAMES[account]+'</h3>')
        parts.append(table(['Статья','Сентябрь, ₽','Октябрь, ₽','Сентябрь, %','Октябрь, %'],[
            [x['label'],rub(x['september']),rub(x['now']),rub(x['september_pct']),rub(x['now_pct'])]
            for x in promo_parts if x['account']==account and (x['now'] or x['september'])]))
    parts.append('<p>Полная месячная подписка в каждом кабинете:6 990 ₽ Premium Plus +28 000 ₽ «Аналитика Plus» =34 990 ₽ вместо24 990 ₽. Рост10 000 ₽ на кабинет, суммарно20 000 ₽/месяц. Она уже входит в удержания, повторно её не добавляем. «Баллы за отзывы» — денежная услуга продавца; рекламные бонусы Ozon и баллы за скидки покупателям — другие показатели.</p></section><section><h2>7. Сопоставимые1–17: деньги и рекламные бонусы</h2>')
    aligned=[]
    for account in NAMES:
        for month in ('2026-09','2026-10'):
            m=next(x for x in matched if x['account']==account and x['month']==month)
            f=next(x for x in funding if x['account']==account and x['month']==month)
            aligned.append([NAMES[account],month,rub(m['values']['sales']),rub(m['cash_cpc_cpo']),rub(m['cash_cpc_cpo_pct'])+'%',rub(float(f['bonus']))])
    parts.append(table(['Кабинет','Месяц','Оборот1–17, ₽','Денежные CPC/CPO, ₽','Доля, %','Бонусы списано, справочно'],aligned))
    parts.append('<p>Во всех четырёх сочетаниях кабинет×месяц денежный «Расход» официального Performance CSV сошёлся с финансовыми операциями до копейки. Бонусы не вычитаем из прибыли. Маржу за1–17 не рассчитываем: надёжного согласованного дневного COGS для этого окна нет.</p></section><section><h2>8. Новости Главной как фон</h2><ul><li>С28 сентября снизилась скидка к комиссии за отсрочку выплат. Это возможная часть роста комиссии Цифрового; отдельный эффект тарифа и изменения ассортимента не изолирован.</li><li>С29 сентября возвращена плата10 ₽ за отправление при отдельных способах приёмки в СЦ. Изменения способа приёмки и фактические начисления требуют отдельной сверки; всю динамику доставки этой новостью не объясняем.</li><li>С1 октября аналитика переведена в «Озон Технологии». Два списания28 000 ₽ расшифрованы и учтены как фикс-подписка.</li><li>Удешевление «Сбора первых отзывов» с26 октября и новые тарифы логистики с30 октября ещё не действуют в этом срезе.</li><li>Отмену рекомендованного слота6 октября перенесли на19 ноября. Не используем её как причину текущих расходов.</li></ul><p><a href="news_pult.json">Сохранённые тексты новостей Пульта</a>.</p></section><section><h2>9. Что проверять первым</h2><ol><li>Цифровой: согласовать даты обновления себестоимости и финансов. До этого20,31% — показатель интерфейса, а не подтверждённая итоговая маржа.</li><li>Цифровой: проверить отдачу CPC/CPO — денежная рекламная нагрузка выросла на сопоставимых датах.</li><li>Дисквер: сверить себестоимость и сторно по крупным возвратам, состав товаров в платных первых отзывах и пользу подписки34 990 ₽.</li></ol>')
    missing=[]
    for account in NAMES:
        cov=next(x for x in coverage if x['account']==account and x['period_from']=='2026-10-01')
        missing.append(f"{NAMES[account]}: {cov['missing_skus']} SKU с выручкой{rub(float(cov['revenue_without_cogs']))} ₽ без положительной COGS")
    parts.append('<p class="note">Дополнительно в рассчитанной части витрины остаются пропуски: '+ '; '.join(missing)+'. Себестоимость не оценивали и не подставляли.</p><p>Проверка: <a href="pult_current.json">живой ответ Пульта</a>, <a href="monthly.json">суммы и детализация</a>, <a href="comparison.json">вклады маржи</a>, <a href="since_previous_analysis.json">изменения против прошлого среза</a>, <a href="verification.json">сверки</a>. Работа только на чтение, финансовые данные и Пульт не менялись.</p></section></main></html>')
    text = ''.join(parts)
    note = ('<p class="note" id="first-reviews-disabled">Уточнение пользователя20.10: '
            '«Сбор первых отзывов» отключён в обоих кабинетах. Уже начисленная плата '
            'сохраняется в расходах; проверяем только остаточные начисления. '
            '<a href="../fin5_ozon_cogs_ads_2026-10-20/report.html">Причина отставания COGS '
            'и подробная отдача по рекламным кампаниям</a>.</p>')
    anchor = '<h2>4. Дисквер: возвраты, первые отзывы и полная подписка</h2>'
    text = text.replace(anchor, anchor + note, 1)
    text = text.replace('состав товаров в платных первых отзывах и пользу подписки34 990 ₽',
                        'остаточные начисления отключённых первых отзывов и пользу подписки34 990 ₽')
    (OUT / 'report.html').write_text(text)
    assert hashes == {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in protected}
    with (OUT / 'summary.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['account','month','finance_last_date','turnover','mp_costs','cogs','net_before_opex_tax','margin_pct'])
        for x in monthly:
            v=x['values'];writer.writerow([x['account'],x['month'],x.get('last_date','2026-09-30'),v['sales'],v['itog'],v['cogs'],v['net'],v['margin']])
    print('Built new report. 14 Pult cells, 2 margin bridges, 8 expense details and 4 cash exports reconciled.')


if __name__ == '__main__':
    main()
