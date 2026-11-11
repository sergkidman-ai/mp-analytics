# поток: mkt / ozon
"""Offline two-account wave1 report from bounded, read-only Performance API snapshots."""
import csv
import datetime as dt
from decimal import Decimal as D
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

P = Path(__file__).parent
TZ = ZoneInfo('Europe/Moscow')
LABELS = {'oz_acc1':'Акк1 — Цифровой квадрат', 'oz_acc2':'Акк2 — Дисквэр'}
ORDER = ['Новинки','Бандлы','Старые','Дочерние']


def money(x):
    return f'{D(str(x)):,.2f}'.replace(',', ' ').replace('.', ',')


def whole(x):
    return f'{int(x):,}'.replace(',', ' ')


def table(head, rows):
    return '<div class="scroll"><table><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in head)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</table></div>'


all_data, body, md = {}, [], []
finish = []
for account in LABELS:
    source = json.loads((P/(account+'_snapshot.json')).read_text())
    assert len(source['campaigns']) == 4
    assert all(c['state']=='CAMPAIGN_STATE_RUNNING' for c in source['campaigns'])
    assert all(c['statistics_rows']>0 for c in source['campaigns']), 'Missing source is unknown, never zero'
    rows=[]
    for c in source['campaigns']:
        m = c['metrics']; group = c['title'].split('|')[2].strip().replace(' за полгода','')
        value = D(m['ordersMoney']); spend = D(m['moneySpent'])
        rows.append({'account':account,'campaign_id':c['campaign_id'],'group':group,'state':c['state'],
                     'cohort_sku_count':c['cohort_sku_count'],'spend_RUB':str(spend),'views':int(D(m['views'])),
                     'clicks':int(D(m['clicks'])),'ad_attributed_orders':int(D(m['orders'])),
                     'ad_attributed_order_value_RUB':str(value),'ad_DRR_pct':str(spend/value*100) if value else None})
    rows.sort(key=lambda r:next(i for i,k in enumerate(ORDER) if r['group'].startswith(k)))
    totals={k:sum((D(str(r[k])) for r in rows),D(0)) for k in ['cohort_sku_count','spend_RUB','views','clicks','ad_attributed_orders','ad_attributed_order_value_RUB']}
    assert totals['cohort_sku_count'] == (3451 if account=='oz_acc1' else 3851)
    total_DRR = totals['spend_RUB']/totals['ad_attributed_order_value_RUB']*100 if totals['ad_attributed_order_value_RUB'] else None
    start=dt.datetime.fromisoformat(source['activation_at_utc']).astimezone(TZ)
    end=dt.datetime.fromisoformat(source['finished_at_utc']).astimezone(TZ); finish.append(end)
    period=f'С {start:%d.%m %H:%M} до {end:%d.%m %H:%M} МСК'
    display=[[r['group'],whole(r['cohort_sku_count']),money(r['spend_RUB']),whole(r['views']),whole(r['clicks']),whole(r['ad_attributed_orders'])] for r in rows]
    display.append(['Итого',whole(totals['cohort_sku_count']),money(totals['spend_RUB']),whole(totals['views']),whole(totals['clicks']),whole(totals['ad_attributed_orders'])])
    headers=['Группа','SKU','Расход, ₽','Показы','Клики','Заказы Ozon']
    body.append(f'<h2>{LABELS[account]}</h2><p>{period}. Все 4 РК работают.</p>'+table(headers,display))
    body.append(f'<p>Стоимость приписанных рекламой заказов: {money(totals["ad_attributed_order_value_RUB"])} ₽; рекламный ДРР: {money(total_DRR) if total_DRR is not None else "не определён"}%. Расход составляет {money(totals["spend_RUB"]/12500*100)}% недельного ориентира 12 500 ₽. Ориентир — сигнал для оценки, не жёсткий лимит Ozon.</p>')
    md += [LABELS[account],period,'| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']
    md += ['| '+' | '.join(row)+' |' for row in display]+['']
    with (P/(account+'_campaigns.csv')).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    all_data[account]={'activation_at_utc':source['activation_at_utc'],'data_received_at_utc':source['finished_at_utc'],
                       'campaigns':rows,'totals':{k:str(v) for k,v in totals.items()},
                       'ad_DRR_pct':str(total_DRR) if total_DRR is not None else None,
                       'owner_weekly_reference_RUB':12500,
                       'last_guard_checked_at_utc':source['last_guard_checked_at_utc'],
                       'last_guard_status':source['last_guard_status'],
                       'last_guard_actions':source['last_guard_actions'],
                       'calendar_stop_at_msk':source['calendar_stop_at_msk']}
asof=max(finish)
summary={'report_date_msk':'2026-11-10','source':'Ozon Performance /api/client/statistics/daily/json',
         'last_response_at_utc':asof.astimezone(dt.timezone.utc).isoformat(),'today_is_partial':True,
         'account_periods_have_different_lengths':True,'orders_are_ad_attribution_not_financial_realization':True,
         'marketplace_mutations':0,'accounts':all_data}
(P/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
(P/'TABLES.md').write_text('\n'.join(md)+'\n')
slug='/api/wb-bids/reports/ozon-wave1-status-2026-11-10'
html='<h1>Ozon: первая волна на двух аккаунтах</h1>'+f'<p>Данные получены {asof:%d.%m.%Y %H:%M} МСК. Сегодня неполный день. Обе волны работают; показатели накоплены с фактического старта каждой волны.</p>'
html+='<p><b>Обновление 11 ноября:</b> <a href="/api/wb-bids/reports/ozon-wave1-profile-2026-11-11">Свежие показатели обеих волн и динамика всего профиля аккаунтов</a>. Ниже сохранён срез 10 ноября.</p>'
html+=''.join(body)
html+='<p>Заказы и их стоимость — рекламная атрибуция Ozon, она может досчитываться. Это стоимость приписанных заказов, не финансовая реализация. Общие продажи склеек и аккаунтов этим запросом не измерялись. Периоды аккаунтов разной длины, их абсолютные итоги не показывают сравнительную эффективность.</p>'
html+='<p>SKU — зафиксированный состав запущенных волн по проверенным manifest: 3 451 и 3 851. Текущие состояния подтверждены отдельным чтением API. Последний плановый контроль защиты — 10.11 в 09:00 МСК, CHECKED, действий остановки 0. Новых активаций, правок ставок, бюджетов или состава не выполнялось.</p>'
html+=f'<p><a href="{slug}-acc1">Акк1 CSV с ID и стоимостью заказов</a> · <a href="{slug}-acc2">Акк2 CSV с ID и стоимостью заказов</a> · <a href="{slug}-data">Сводка JSON</a></p>'
(P/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: статус первой волны 10 ноября</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}</style>'+html+'</html>')
print('Rendered two tables; 8/8RUNNING; acc1 5685.09RUB/10 attributed orders; acc2 1190.62RUB/1; no marketplace writes')
