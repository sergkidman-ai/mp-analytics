import os
os.environ['PGOPTIONS']='-c default_transaction_read_only=on'
import sys,json,csv
sys.path.insert(0,'/opt/mp-analytics')
from pathlib import Path
from core import db
from reports import ozon_mp_report as O,yandex_mp_report as Y
P=Path('/opt/mp-analytics/docs/reports/fin5_oct_2026-10-19')
def save(n,x):(P/n).write_text(json.dumps(x,default=str,ensure_ascii=False,indent=2))
rows=json.loads((P/'monthly.json').read_text());comparisons=json.loads((P/'comparison.json').read_text())
for c in comparisons:
 if c['platform']=='wb':
  now=next(r['values'] for r in rows if r['account']==c['account'] and r['month']=='2026-10');old=next(r['values'] for r in rows if r['account']==c['account'] and r['month']=='2026-09')
  for x in c['components']:
   if x['key']=='returns_pay':
    x.update(key='returns',now=now['returns'],prev=old['returns'],pct=100*now['returns']/now['own_price'],prev_pct=100*old['returns']/old['own_price']);x['impact_pp']=x['prev_pct']-x['pct']
  c['components'].sort(key=lambda x:x['impact_pp'])
 assert abs(sum(x['impact_pp'] for x in c['components'])-(c['margin']-c['prev_margin']))<1e-8,c['account']
save('comparison.json',comparisons)
actual=next(r['values'] for r in rows if r['account']=='ya_acc1' and r['month']=='2026-10')
payments=json.loads((P/'yandex_calendar_comparison.json').read_text());cal=next(r for r in payments if r['month']=='2026-10')
corrected={**actual,'revenue':float(cal['receipts']),'returns_money':float(cal['refunds'])}
audit=Y._derive(corrected,actual['orders'],actual['returns_cnt'],actual['cogs'])
audit['original_display']={k:actual[k] for k in ['revenue','returns_money','own','itog','net','margin','cogs']}
audit['net_bridge']=dict(add_gross_receipts=audit['revenue']-actual['revenue'],subtract_additional_refunds=audit['returns_money']-actual['returns_money'],net_increase=audit['net']-actual['net'])
audit['status']='audit_only_not_written_to_pult_or_db; calendar payments from existing raw stats; COGS unchanged'
save('yandex_audit_bridge.json',audit)
matched=[]
for acc in ['oz_acc1','oz_acc2']:
 for month in ['2026-09','2026-10']:
  mags,sub,det,sc=O._balance_range(acc,month+'-01',month+'-19');ob=mags['sales'];cost=sum(mags[k] for k in O.EXP_SVC)
  matched.append(dict(account=acc,month=month,days='01-18',sales=ob,services=cost,cost_pct=cost/ob*100,commission_pct=mags['commission']/ob*100,promo_pct=mags['promo']/ob*100,returns_pct=mags['returns']/ob*100,details=det))
save('matched_18_days.json',matched)
matched_ya=[]
payrows=json.loads((P/'yandex_calendar_payments.json').read_text())
for month in ['2026-09','2026-10']:
 r=db.query("SELECT sum(cost) cash,sum(netting) netting FROM raw_yandex_services WHERE ym=%s AND svc_date>=%s AND svc_date<%s",(month,month+'-01',month+'-19'))[0]
 receipts=sum(float(p['amount']) for p in payrows[month] if p['type']=='PAYMENT' and p['date']<=month+'-18');refunds=sum(float(p['amount']) for p in payrows[month] if p['type']=='REFUND' and p['date']<=month+'-18')
 gross=receipts+float(r['netting'] or 0);cost=float(r['cash'] or 0)+float(r['netting'] or 0)
 matched_ya.append(dict(month=month,days='01-18',cash_services=float(r['cash'] or 0),netting=float(r['netting'] or 0),receipts=receipts,refunds=refunds,gross_turnover=gross,accrued_services_pct=cost/gross*100,scope='raw service categories; agency separate'))
save('yandex_matched_18_days.json',matched_ya)
ads=db.query("SELECT account,to_char(stat_date,'YYYY-MM') AS period,campaign_id,sum(money_spent) spend,sum(clicks) clicks,sum(orders_qty) orders,sum(orders_money) attributed_revenue FROM mkt_ozon_ads_sku_daily WHERE stat_date>='2026-09-01' AND stat_date<='2026-10-18' AND extract(day from stat_date)<=18 GROUP BY 1,2,3 ORDER BY 1,2,4 DESC")
save('ads_matched_18_days.json',ads)
with (P/'summary.csv').open('w',newline='') as f:
 w=csv.writer(f);w.writerow(['account','as_of','turnover_oct','profit_oct_before_opex_tax','margin_oct_pct','margin_sep_pct','market_cost_oct_pct','market_cost_sep_pct','cogs_oct_pct','cogs_sep_pct'])
 for c in comparisons:
  r=next(r for r in rows if r['account']==c['account'] and r['month']=='2026-10');w.writerow([c['account'],r['last_date'],c['turnover'],c['net'],c['margin'],c['prev_margin'],c['market_cost_pct'],c['prev_market_cost_pct'],c['cogs_pct'],c['prev_cogs_pct']])
print('YA audit gross',round(audit['own'],2),'net',round(audit['net'],2),'margin',round(audit['margin'],3),'bridge',audit['net_bridge'])
for r in matched:print('matched Ozon',r['account'],r['month'],'cost%',round(r['cost_pct'],3),'promo%',round(r['promo_pct'],3),'commission%',round(r['commission_pct'],3))
for r in matched_ya:print('matched YA',r['month'],'cost%',round(r['accrued_services_pct'],3),'cash sales',r['receipts'])
print('Five margin decompositions reconcile; audit bridge saved; database unchanged.')
