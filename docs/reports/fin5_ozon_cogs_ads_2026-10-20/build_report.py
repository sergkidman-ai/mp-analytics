# поток: fin
"""Отдача CPC по сохранённым официальным источникам; без рыночных записей."""
import csv
import html
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
NAMES = {'oz_acc1': 'Цифровой', 'oz_acc2': 'Дисквер'}


def load(name):
    return json.loads((OUT / name).read_text())


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def ratio(a, b, factor=1):
    return factor*a/b if b else None


def fmt(value):
    return 'нет данных' if value is None else f'{value:,.2f}'.replace(',', ' ').replace('.', ',')


def table(headers, rows):
    def cells(values, tag):
        return ''.join(f'<{tag}>{html.escape(str(v))}</{tag}>' for v in values)
    return '<div class="scroll"><table><thead><tr>'+cells(headers,'th')+'</tr></thead><tbody>'+''.join(
        '<tr>'+cells(r,'td')+'</tr>' for r in rows)+'</tbody></table></div>'


def main():
    rows = load('campaign_comparison.json')
    discrepancies, totals = [], []
    for row in rows:
        row['media_cost'] = row['cash']+row['bonus']
        row['cash_drr'] = ratio(row['cash'],row['revenue'],100)
        row['media_drr'] = ratio(row['media_cost'],row['revenue'],100)
        row['cash_per_attributed_order'] = ratio(row['cash'],row['orders'])
        row['media_cpc'] = ratio(row['media_cost'],row['clicks'])
        row['conversion_pct'] = ratio(row['orders'],row['clicks'],100)
        row['spend_source_difference'] = row['budget_spend']-row['media_cost']
        if abs(row['spend_source_difference']) > .02:
            discrepancies.append({k:row[k] for k in ['account','month','campaign_id','budget_spend',
                                                     'cash','bonus','spend_source_difference']})
        # Эти прежние поля использовали расход daily/json; дальше расход только expense CSV.
        row.pop('budget_drr',None)
        row.pop('cpc',None)
    for account in NAMES:
        for month in ('2026-09','2026-10'):
            selected=[x for x in rows if x['account']==account and x['month']==month and x['model']=='CPC']
            total={k:sum(x[k] for x in selected) for k in
                   ['cash','bonus','media_cost','views','clicks','orders','revenue']}
            total.update(account=account,month=month)
            total['cash_drr']=ratio(total['cash'],total['revenue'],100)
            total['media_drr']=ratio(total['media_cost'],total['revenue'],100)
            total['cash_per_attributed_order']=ratio(total['cash'],total['orders'])
            total['conversion_pct']=ratio(total['orders'],total['clicks'],100)
            total['media_cpc']=ratio(total['media_cost'],total['clicks'])
            totals.append(total)
    save('campaign_comparison.json',rows)
    save('cpc_totals.json',totals)
    save('expense_daily_discrepancies.json',discrepancies)
    cash_growth = {}
    for account in NAMES:
        before=next(x for x in totals if x['account']==account and x['month']=='2026-09')
        now=next(x for x in totals if x['account']==account and x['month']=='2026-10')
        cash_growth[account]=now['cash']-before['cash']
    comparisons=[]
    for now in rows:
        if now['month']!='2026-10':continue
        old=next((x for x in rows if x['account']==now['account'] and
                  x['month']=='2026-09' and x['campaign_id']==now['campaign_id']),None)
        comparisons.append(dict(account=now['account'],campaign_id=now['campaign_id'],
            title=now.get('title',''),model=now['model'],state=now['state'],
            cash_change=now['cash']-(old['cash'] if old else 0),
            october=now,september=old))
    save('campaign_changes.json',comparisons)
    assert len([x for x in rows if x['model']=='CPO'])==4
    assert all(x['revenue']==0 and x['orders']==0 for x in rows if x['model']=='CPO')
    a=next(x for x in comparisons if x['campaign_id']=='12704286')
    b=next(x for x in comparisons if x['campaign_id']=='13424726')
    facts={'first_customer_reviews':{'status':'disabled_in_both_accounts',
             'source':'Подтверждено пользователем 20.10.2026; дата отключения не указана',
             'accrued_costs_remain_expenses':True},
           'cogs_lag':{'posting_http_status':502,'last_run_started':'2026-10-19 19:07 UTC',
             'run_still_active_at_audit':True,'next_run_skipped':'2026-10-20 02:07 UTC',
             'blocked_stage':'Ozon постинги oz_acc2 2026-10-01..2026-10-31',
             'digital_margin_skipped_due_to_postings_failure':True,
             'moysklad_digital_shipment_cost_collection_succeeded':True,
             'unbounded_429_retry_exists_in_collector':True,
             'actual_429_responses_from_hung_process_not_logged_or_proven':True},
           'priorities':{'oz_acc1':a['campaign_id'],'oz_acc2':b['campaign_id']},
           'experiment_cash_growth':a['cash_change'],
           'experiment_share_of_net_cpc_cash_growth_pct':100*a['cash_change']/cash_growth['oz_acc1'],
           'all_campaign_share_of_net_cpc_cash_growth_pct':100*b['cash_change']/cash_growth['oz_acc2'],
           'campaigns_changed':False,'production_or_database_changed':False}
    save('conclusions.json',facts)
    content=['''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: отставание себестоимости и отдача рекламы</title><style>body{font:16px/1.6 system-ui;background:#f5f7fb;color:#1b293d;margin:0}main{max-width:1250px;margin:auto;padding:24px}section{background:white;border:1px solid #dce3ed;border-radius:12px;margin:18px 0;padding:20px}h1{font-size:28px;line-height:1.3}h2{font-size:22px}h3{font-size:18px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:9px;text-align:right;border-bottom:1px solid #e0e5ee;white-space:nowrap}th{background:#edf2f8}th:first-child,td:first-child{text-align:left}.warn{background:#fff0d6;padding:16px;border-left:5px solid #d49d29}.note{color:#596a80}a{color:#125ca9}code{overflow-wrap:anywhere}@media(max-width:600px){main{padding:12px}section{padding:13px}h1{font-size:24px}}</style><main><h1>Ozon: почему отстала себестоимость и как работает реклама</h1><p>Проверка20.10.2026. Реклама — одинаковые1–17 сентября и октября; расходы из официального Performance expense CSV, заказы/выручка из свежего daily/json. Состояния кампаний — на момент проверки.</p><p class="warn"><b>Качество отдачи ухудшилось у обоих кабинетов, причины разные.</b> У Цифрового основной рост денежного CPC сосредоточен в «Эксперимент1». У Дисквера ухудшилась обычная кампания «Все», а большой общий рекламный бюджет содержит бонусы. ДРР показывает расход относительно приписанной Ozon выручки, а не чистую прибыль.</p><section><h2>1. Причина отставания себестоимости</h2><p>Вечером19.10 Ozon вернул502 Bad Gateway на /v3/posting/fbs/list при загрузке заказов Цифрового за сентябрь и октябрь. Начисления финансов загрузились отдельно, себестоимость отгрузок из МойСклада также собрана успешно. Но run_daily намеренно пропустил обновление витрины маржи Цифрового: не все источники прошли проверку. Поэтому финансовый оборот обновился, а COGS в витрине сохранилась прежней.</p><p>Тот же вечерний прогон19.10 с19:07UTC к моменту проверки не завершился: последняя запись — начало загрузки заказов Дисквера за октябрь. Процесс жив более18часов. Ночной запуск20.10 в02:07UTC был пропущен: предыдущий процесс удерживал блокировку. В коде есть бесконечные повторы429 с ожиданием Retry-After и нет общего лимита времени этапа. Какие ответы получал именно зависший запрос, журнал не пишет; утверждать, что эти18часов вызваны именно429, нельзя.</p><p>Это технический сбой обновления, а не подтверждённое отсутствие себестоимости в МойСкладе. Для восстановления нужны ограничение времени/повторов сбора, завершение зависшего прогона и согласованная загрузка заказов и пересборка COGS. До этого20,31% маржи Цифрового предварительны. <a href="cogs_pipeline_events.txt">Этапы прогонов</a>, <a href="cogs_failure_summary.json">Ошибки Ozon</a>, <a href="skipped_runs.json">Пропуск ночного запуска</a>.</p></section><section><h2>2. Первые отзывы — отключённая услуга</h2><p>По подтверждению пользователя услуга отключена в обоих кабинетах; своевременно не замеченное подключение создалo уже начисленные расходы. Эти суммы сохраняем в затратах: Цифровой5 034,82 ₽, Дисквер36 966,14 ₽ в текущем финансовом срезе. Рассматривать её как продолжающуюся управляемую рекламную кампанию не нужно. Дату отключения пользователь не указал; последние начисления сами по себе не доказывают повторное включение.</p></section><section><h2>3. Отдача денежного CPC — общий результат</h2>''']
    summary=[]
    for x in totals:
        summary.append([NAMES[x['account']],x['month'],fmt(x['cash']),fmt(x['bonus']),
            int(x['orders']),fmt(x['revenue']),fmt(x['cash_drr'])+'%',
            fmt(x['cash_per_attributed_order']),fmt(x['conversion_pct'])+'%'])
    content.append(table(['Кабинет','1–17 месяца','Наши деньги, ₽','Бонусы, ₽','Заказы Ozon','Выручка Ozon, ₽','Денежный ДРР','Наших ₽ / заказ','Заказы / клики'],summary))
    content.append('<p>Цифровой: денежный CPC303 124,52→418 618,49 ₽ (+38,10%), заказы717→915 (+27,62%), выручка5 848 988→6 772 727 ₽ (+15,79%). ДРР5,18→6,18%, стоимость приписанного заказа422,77→457,51 ₽. Клики выросли11 481→18 359, но конверсия6,25→4,98%.</p><p>Дисквер: денежный CPC19 128,53→29 673,29 ₽ (+55,13%), заказы128→93 (−27,34%), выручка674 233→503 622 ₽ (−25,30%). ДРР2,84→5,89%, наших денег на приписанный заказ149,44→319,07 ₽. Ранее сравнённая сумма CPC+CPO скрывала ухудшение CPC: расходы CPO у Дисквера сократились.</p><p class="note">В заказах/выручке есть результаты всей кампании, включая показы за бонусы. Поэтому денежный ДРР смешанных кампаний не является эффективностью только платного трафика. Октябрьские заказы могут ещё доатрибутироваться; сентябрь более зрелый. Приписанная выручка не равна инкрементальной прибыли, не очищена здесь от последующих возвратов и всех расходов заказа.</p></section>')
    content.append('<section><h2>4. Цифровой — приоритет «Эксперимент1»</h2>')
    content.append(f"<p>Кампания12704286 сейчас RUNNING. Наши деньги129 000,24→264 483 ₽ (+105,02%), заказы306→468, выручка2 052 892→2 762 061 ₽. ДРР6,28→9,58%, наших денег на заказ421,57→565,13 ₽. Рост расходов{fmt(a['cash_change'])} ₽ объясняет{fmt(facts['experiment_share_of_net_cpc_cash_growth_pct'])}% чистого увеличения денежного CPC: остальные кампании суммарно часть роста компенсировали.</p>")
    content.append('<p>Приоритет — разобрать товары и ставки этой кампании, которые получили дополнительный бюджет, и сопоставить их с заказами после возвратов и корректной себестоимостью. «От0до1000» — второй риск: ДРР10,76→17,20%, заказы46→39 при росте наших затрат3 522,49→6 429,05 ₽. По эффективности расходов лучше выглядят «От2000до5000» (ДРР5,13→3,51%) и «От10000до500тыс» (октябрь3,95%); это ещё не доказательство чистой прибыли.</p></section><section><h2>5. Дисквер — приоритет «Все»</h2><p>Кампания13424726 сейчас RUNNING. Денежные расходы12 969,35→22 308,10 ₽ (+72,01%), заказы77→49 (−36,36%), выручка412 120→251 766 ₽ (−38,91%). ДРР3,15→8,86%, наших денег на заказ168,43→455,27 ₽. В октябре добавлены1 360,92 ₽ бонусов, поэтому полный расход по этой кампании23 669,02 ₽ и полный ДРР9,40%.</p><p>Кампания «От0до3000»: затраты2 497,66→3 973,01 ₽, заказов по28, денежный ДРР5,15→7,04%. «10000–500000» показывает низкий ДРР0,91%, но всего7 заказов — малый объём для решения о масштабировании. Приоритет для проверки — «Все»: на неё приходится около88,6% прироста денежного CPC Дисквера.</p></section>')
    content.append('<section><h2>6. Все CPC-кампании: сопоставление</h2><p>Отдельно показаны наши деньги и бонусы. Прочерк/«нет данных» при отсутствии приписанной выручки не означает нулевой расход.</p>')
    for account in NAMES:
        content.append('<h3>'+NAMES[account]+'</h3>')
        selected=sorted([x for x in comparisons if x['account']==account and x['model']=='CPC'],
                        key=lambda x:x['october']['cash'],reverse=True)
        table_rows=[]
        for row in selected:
            x,y=row['october'],row['september'] or {}
            table_rows.append([row['title']+' · '+row['campaign_id'],fmt(y.get('cash',0)),fmt(x['cash']),
                fmt(x['bonus']),int(y.get('orders',0)),int(x['orders']),fmt(x['revenue']),
                fmt(y.get('cash_drr')),fmt(x['cash_drr']),fmt(x['media_drr']),row['state'].replace('CAMPAIGN_STATE_','')])
        content.append(table(['Кампания','Сентябрь деньги, ₽','Октябрь деньги, ₽','Октябрь бонусы, ₽',
            'Заказы сент.','Заказы окт.','Выручка окт., ₽','ДРР сент., %','ДРР окт., %',
            'ДРР деньги+бонусы окт., %','Состояние'],table_rows))
    content.append('<p>Название кампании не определяет источник средств. Например, «Вне РК — спрос без рекламы» у Цифрового действительно потратила12 481,01 ₽ наших денег за1–17 октября.</p></section><section><h2>7. Бонусы Дисквера и проверка источников</h2><p>Из139 030,05 ₽ бонусов октября основная часть ушла в кампании с очень малой приписанной отдачей:41758909 —48 690,74 ₽ бонусов и2 заказа на7 772 ₽;43608717 —62 867,35 ₽ и0заказов;42757736 —24 676 ₽ и0заказов, уже INACTIVE. Их собственные денежные расходы — копейки/единицы рублей. Это низкая отдача бонусного бюджета, не такие же потери наших денег.</p><p>На кампании41758909 расход daily/json выше суммы денег и бонусов expense CSV на1 360,92 ₽. Назначение расхождения отдельно не установлено. Чтобы не задвоить затраты, все показатели расходов и ДРР здесь рассчитаны по expense CSV; daily/json используется для показов, кликов, заказов и выручки. <a href="expense_daily_discrepancies.json">Сверка расхождения</a>.</p></section><section><h2>8. Оплата за заказ — расход есть, связанной выручки в API нет</h2>')
    cpo=[]
    for account in NAMES:
        for month in ('2026-09','2026-10'):
            x=next(x for x in rows if x['account']==account and x['month']==month and x['model']=='CPO')
            cpo.append([NAMES[account],month,fmt(x['cash']),'API не возвращает','не рассчитывается'])
    content.append(table(['Кабинет','1–17 месяца','Денежный CPO, ₽','Связанные заказы/выручка','ДРР кампании'],cpo))
    content.append('<p>Нули orders/ordersMoney у CPO — ограничение используемого отчёта, не доказательство отсутствия заказов. В Цифровом CPO —53,1% наших рекламных расходов, в Дисквере68,5%. Для оценки чистой окупаемости этой части нужны связанные с инструментом заказы/товары, подтверждённые продажи и возвраты, комиссии и себестоимость. Нельзя прибавлять её расходы к CPC и делить на выручку только CPC: это даст ложный ДРР.</p></section><section><h2>9. Что можно и нельзя решить по этим данным</h2><p>Подтверждённые приоритеты: «Эксперимент1» Цифрового и «Все» Дисквера. По ним ухудшились затраты на приписанный заказ и ДРР. Результаты по CPC достаточны для предметной проверки ставок и товаров; сами по себе они не доказывают чистый убыток каждой кампании.</p><p>SKU-статистика в БД отстаёт от свежих кампаний: у «Эксперимент1» в ней426заказов вместо свежих468, у «Все»43вместо49. Поэтому списки SKU с нулевыми заказами из этой витрины не использованы как актуальное доказательство бесполезных расходов. Нужна свежая детализация товаров, прежде чем принимать точечные решения.</p><p>Кампании и ставки не менялись. Внешних сообщений нет. Проверка выполнена чтением логов/БД и бесплатными GET Performance API. <a href="campaign_comparison.json">Все показатели кампаний</a>; <a href="cpc_totals.json">Итоги CPC</a>; <a href="campaign_changes.json">Изменения</a>; <a href="conclusions.json">Выводы и статус первых отзывов</a>; <a href="fetch_summary.json">Источники</a>.</p></section></main></html>')
    (OUT/'report.html').write_text(''.join(content))
    with (OUT/'campaign_comparison.csv').open('w',newline='') as f:
        fields=['account','month','campaign_id','title','model','state','cash','bonus','media_cost',
                'clicks','orders','revenue','cash_drr','media_drr','cash_per_attributed_order','conversion_pct']
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    print('Built report: 4 CPC totals, 4 CPO rows,',len(comparisons),'October campaigns;',len(discrepancies),'expense/daily discrepancy.')


if __name__=='__main__':
    main()
