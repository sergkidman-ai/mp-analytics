# поток: mkt / ozon
"""Combine captured campaign-day evidence; request SKU reports only for months/campaigns with activity."""
import calendar
import csv
import datetime as dt
import decimal
import json
import pathlib

OUT=pathlib.Path(__file__).parent
REC=OUT.parent/'ozon_recovery_20261103'
D=decimal.Decimal
campaigns=json.loads((OUT.parent/'ozon_full_inventory_20261105/current_campaigns.json').read_text())['campaigns']
meta={str(c['id']):c for c in campaigns}
sources=list((OUT/'raw').glob('campaign_daily_*.json'))+list((REC/'raw').glob('daily_*.json'))+list((REC/'daily_20261105/account_comparison').glob('api_response_*.json'))
index={};captured={}
def number(value):return D(str(value or 0).replace('\xa0','').replace(' ','').replace(',','.'))
def date(value):
    return dt.datetime.strptime(value,'%d.%m.%Y').date().isoformat() if '.' in value else value[:10]
for f in sources:
    z=json.loads(f.read_text())
    if z.get('http')!=200:continue
    body=z.get('body',{})
    if not isinstance(body,dict) or not isinstance(body.get('rows'),list):continue
    stamp=z.get('at_utc') or z.get('retrieved_at_utc') or ''
    for r in body['rows']:
        cid,day=str(r['id']),date(r['date'])
        if not '2025-11-05'<=day<='2026-11-04':continue
        key=(cid,day)
        if key in captured and captured[key]>stamp:continue
        c=meta.get(cid,{})
        index[key]={'campaign_id':cid,'date_msk':day,'type':c.get('advObjectType'),'payment_type':c.get('PaymentType'),
                    'views':str(number(r.get('views'))),'clicks':str(number(r.get('clicks'))),
                    'spend_rub':str(number(r.get('moneySpent'))),'ad_orders_api':str(number(r.get('orders'))),
                    'ad_sales_rub':str(number(r.get('ordersMoney'))),'snapshot_utc':stamp,'source':str(f.resolve())}
        captured[key]=stamp
rows=sorted(index.values(),key=lambda r:(r['date_msk'],r['campaign_id']))
with (OUT/'campaign_daily_year.csv').open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
jobs=[]
windows=[('2025-11-05','2025-11-30'),('2025-12-01','2025-12-31')]
windows += [(f'2026-{m:02}-01',f'2026-{m:02}-{calendar.monthrange(2026,m)[1]}') for m in range(1,10)]
windows += [('2026-10-01','2026-10-01'),('2026-11-01','2026-11-04')]
for first,last in windows:
    ids=sorted({r['campaign_id'] for r in rows if first<=r['date_msk']<=last and r['type']=='SKU' and r['payment_type']=='CPC' and (number(r['views'])>0 or number(r['clicks'])>0 or number(r['spend_rub'])>0)})
    for i in range(0,len(ids),10):
        jobs.append({'key':f'sku_{first}_{last}_{i//10:02}','request':{'campaigns':ids[i:i+10],'dateFrom':first,'dateTo':last,'groupBy':'DATE'}})
reuse=[str(f.resolve()) for f in sorted((REC/'raw').glob('sku_report_2026-10-02_2026-10-31_*_result.json'))]
(OUT/'sku_report_jobs.json').write_text(json.dumps({'jobs':jobs,'reused_sku_report_files':reuse,'campaign_day_rows':len(rows),'explicit_dates':len({r['date_msk'] for r in rows}),'year_days_expected':365},ensure_ascii=False,indent=2)+'\n')
print('Annual campaign-day rows',len(rows),'distinct dates',len({r['date_msk'] for r in rows}),'/365; planned SKU reports',len(jobs),'reused October reports',len(reuse))
