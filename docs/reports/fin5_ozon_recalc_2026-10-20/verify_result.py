# поток: fin
"""Verify the October recovery against raw finance and live Pult."""
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

import requests
from core import db
from reports import ozon_mp_report as O

OUT = Path(__file__).resolve().parent
NAMES = {'oz_acc1':'Цифровой','oz_acc2':'Дисквер'}


def save(name, value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))


def fmt(value):
    return f'{value:,.2f}'.replace(',',' ').replace('.',',')


def main():
    monitor=json.loads((OUT/'monitor_result.json').read_text())
    assert monitor['status']=='both_october_calculations_succeeded',monitor
    response=requests.get('http://127.0.0.1:8090/api/ozon/mp-current',timeout=60)
    response.raise_for_status()
    api=response.json();save('pult_after.json',api)
    before=json.loads((OUT/'values_before.json').read_text())
    rows=db.query('''SELECT account,period_from,period_to,count(*) n,sum(cogs) cogs,
           sum(revenue_buyer) revenue,min(computed_at) first_computed,max(computed_at) last_computed,
           count(*) filter(where revenue_buyer>0 and coalesce(cogs,0)<=0) missing_skus,
           coalesce(sum(revenue_buyer) filter(where revenue_buyer>0 and coalesce(cogs,0)<=0),0) missing_revenue
        FROM margin_by_sku WHERE platform='ozon' AND period_from='2026-10-01'
        GROUP BY 1,2,3 ORDER BY 1''')
    save('cogs_after.json',rows)
    updated=[]
    hist=O._hist();ix=hist['period_keys'].index('2026-09')
    for acc in NAMES:
        source=next(x for x in rows if x['account']==acc)
        assert source['first_computed']>=dt.datetime(2026,10,20,19,7,tzinfo=dt.timezone.utc)
        mags,subscription,detail,codes=O._accumulate(acc,2026,10)
        orders,returns=O.op_counts(acc,2026,10)
        values=O._derive(mags,orders,returns,O._cogs(acc,2026,10))
        assert abs(float(source['revenue'])-values['sales'])<.01,(acc,source['revenue'],values['sales'])
        for key in ('sales','itog','payout','cogs','net','margin','promo'):
            assert O._fmt(key,values[key],values['sales'])==api['accounts'][acc][key]['jul']['txt'],(acc,key)
        assert abs(detail['promo']['OP:OperationSubscriptionAnalyticsPlus']-28000)<.005
        assert abs(subscription-34990)<.005
        old=next(x['values'] for x in before if x['account']==acc)
        account=hist['accounts'][acc]
        sep=O._derive({k:v[ix] for k,v in account['lines'].items()},account['orders'][ix],
                      account['returns_cnt'][ix],account['cogs'][ix])
        updated.append(dict(account=acc,last_date=str(O._month_last_day(2026,10,acc)),
             before=old,after=values,september=sep,detail=detail,
             changes={k:values[k]-old[k] for k in ('sales','cogs','net','margin')},
             cogs_source=source))
    save('comparison_after.json',updated)
    hist_hash=hashlib.sha256(O.HIST_PATH.read_bytes()).hexdigest()
    save('verification.json',dict(pult_cells_match=True,both_october_vitrines_fresh=True,
         cogs_revenue_equals_finance=True,analytics_subscription_preserved=True,
         closed_ozon_history_unchanged=hist_hash==(OUT/'frozen_before.sha256').read_text().strip(),
         calculation_performed_by=monitor.get('source','already_running_scheduled_run_daily_2026-10-20_19:07'),
         checked_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    page=['''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon — результат пересчёта октября</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:25px auto;padding:0 18px;color:#1d2c40}h1{font-size:27px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border:1px solid #dce3ed;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}.note{background:#fff2d7;padding:14px}a{color:#125bad}</style><h1>Ozon — октябрь после восстановления расчёта себестоимости</h1><p>Изменения wb4c применены. Старый зависший процесс остановлен. Пока общий плановый прогон находился на WB, выполнено точечное обновление Ozon за октябрь: каталог, заказы, начисления, себестоимость отгрузок и витрина. Полный run_daily, историческая заморозка и рассылки этим восстановлением не запускались.</p><div class="scroll"><table><tr><th>Кабинет</th><th>Оборот, ₽</th><th>COGS до, ₽</th><th>COGS после, ₽</th><th>Прибыль до, ₽</th><th>Прибыль после, ₽</th><th>Маржа до</th><th>Маржа после</th><th>Маржа сентября</th></tr>''']
    for x in updated:
        a,b=x['before'],x['after']
        vals=[NAMES[x['account']],fmt(b['sales']),fmt(a['cogs']),fmt(b['cogs']),fmt(a['net']),fmt(b['net']),
              fmt(a['margin'])+'%',fmt(b['margin'])+'%',fmt(x['september']['margin'])+'%']
        page.append('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in vals)+'</tr>')
    page.append('</table></div><p>Прибыль до ОПЕКС и налогов. Новые финансовые начисления тоже могли поступить за время прогона, поэтому изменение прибыли не обязательно равно только изменению COGS.</p><p>При восстановлении дополнительно исправлены два дефекта: обычный запрос FBS v4 теперь использует допустимый лимит100 вместо1000; повторная сборка витрины обновляет computed_at у всех пересчитанных строк.</p><p>В обоих кабинетах сумма выручки SKU-витрины совпала с финансовым оборотом. Плата28 000 ₽ «Аналитика Plus» сохранилась в продвижении; полная фикс-подписка34 990 ₽. Уже начисленные первые отзывы остаются расходом, услуга отключена по подтверждению пользователя.</p>')
    missing=[]
    for x in updated:
        c=x['cogs_source'];missing.append(f"{NAMES[x['account']]}: {c['missing_skus']} SKU, выручка{fmt(float(c['missing_revenue']))} ₽")
    page.append('<p class="note">Оставшиеся позиции без положительной себестоимости: '+'; '.join(missing)+'. Устранение отставания не означает автоматическое заполнение этих пропусков.</p>')
    page.append('<p>Источники: <a href="pult_before.json">Пульт до</a>, <a href="pult_after.json">Пульт после</a>, <a href="comparison_after.json">сравнение</a>, <a href="scoped_result.json">результат этапов</a>, <a href="verification.json">проверки</a>.</p></html>')
    (OUT/'report.html').write_text(''.join(page))
    for x in updated:
        a,b=x['before'],x['after']
        print(x['account'],'COGS',fmt(a['cogs']),'→',fmt(b['cogs']),'profit',fmt(b['net']),
              'margin',fmt(a['margin']),'→',fmt(b['margin']),'vs September',fmt(x['september']['margin']))


if __name__=='__main__':
    main()
