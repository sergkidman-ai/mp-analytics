import os
os.environ['PGOPTIONS']='-c default_transaction_read_only=on'
import sys
sys.path.insert(0,'/opt/mp-analytics')
import json,datetime,calendar
from pathlib import Path
from core import db
from reports import ozon_mp_report as O,wb_mp_report as W,yandex_mp_report as Y

OUT=Path('/opt/mp-analytics/docs/reports/fin5_oct_2026-10-19')
OUT.mkdir(parents=True,exist_ok=True)
def save(n,x): (OUT/n).write_text(json.dumps(x,ensure_ascii=False,default=str,indent=2))
rows=[]
hist={}
for market,mod in [('ozon',O),('wb',W),('yandex',Y)]:
 h=json.loads(Path(f'/opt/mp-analytics/reports/data/mp_{market}_hist.json').read_text());hist[market]=h
 for acc,a in h['accounts'].items():
  for i,ym in enumerate(h['period_keys']):
   if ym<'2026-07':continue
   mags={k:v[i] for k,v in a['lines'].items()}
   values=mod._derive(mags,a['orders'][i],a['returns_cnt'][i],a['cogs'][i])
   rows.append(dict(platform=market,account=acc,month=ym,source='frozen_ui',values=values,stored_net=a['net'][i],detail={ln:{c:v[i] for c,v in codes.items()} for ln,codes in a.get('detail',{}).items()}))
 for acc in mod.ACCOUNTS if market!='yandex' else ['ya_acc1']:
  if market=='ozon':
   mags,sub,detail,sc=mod._accumulate(acc,2026,10);orders,retc=mod.op_counts(acc,2026,10);cogs=mod._cogs(acc,2026,10);last=mod._month_last_day(2026,10,acc)
  elif market=='wb':
   mags=mod.balance(acc,2026,10);detail=mod.balance_detail(acc,2026,10);orders,retc=mod.op_counts(acc,2026,10);cogs=mod._cogs(acc,2026,10);last=mod._month_last_day(2026,10,acc)
  else:
   mags=mod.balance(2026,10);detail={};orders,retc=mod.op_counts(2026,10);cogs=mod._cogs(2026,10);last=mod._last_order_date(2026,10)
  rows.append(dict(platform=market,account=acc,month='2026-10',source='live_ui',last_date=last,values=mod._derive(mags,orders,retc,cogs),detail=detail))
save('monthly.json',rows)
save('yandex_monthly.json',db.query("SELECT * FROM yandex_finance_monthly WHERE account='ya_acc1' AND month>='2026-07-01' ORDER BY month"))
save('news_pult.json',db.query("SELECT platform,account,message_id,created_at,title,body,event_id,chat_type FROM mp_notices WHERE event_id IS NOT NULL AND created_at>='2026-08-01' ORDER BY created_at,account"))
save('events_pult.json',db.query("SELECT id,event_date,kind,platform,account,title,details,expect,source FROM biz_events WHERE event_date>='2026-08-01' AND kind IN ('mp','own') ORDER BY event_date,id"))
save('freshness.json',{
 'ozon':db.query("SELECT account,min((payload->>'operation_date')::date) first_date,max((payload->>'operation_date')::date) last_date,max(loaded_at) loaded_at,count(*) records FROM raw_ozon_transaction WHERE (payload->>'operation_date')::date>='2026-10-01' GROUP BY account"),
 'wb':db.query("SELECT account,min((payload->>'create_dt')::date) first_date,max((payload->>'create_dt')::date) last_date,max(loaded_at) loaded_at,count(*) records FROM raw_wb_report WHERE (payload->>'create_dt')::date>='2026-10-01' GROUP BY account"),
 'ya_services':db.query("SELECT ym,min(svc_date) first_date,max(svc_date) last_date,max(loaded_at) loaded_at,count(*) records,sum(cost) cost,sum(netting) netting FROM raw_yandex_services WHERE ym>='2026-09' GROUP BY ym ORDER BY ym"),
 'ya_closure':db.query("SELECT ym,category,min(transaction_date) first_date,max(transaction_date) last_date,max(loaded_at) loaded_at,count(*) records,sum(amount) amount FROM raw_yandex_closure WHERE ym>='2026-09' GROUP BY ym,category ORDER BY ym,category"),
 'cogs':db.query("SELECT platform,account,period_from,count(*) skus,sum(qty) qty,sum(cogs) cogs,min(computed_at) first_computed,max(computed_at) last_computed FROM margin_by_sku WHERE period_from>='2026-09-01' GROUP BY platform,account,period_from ORDER BY platform,account,period_from")})
comparisons=[]
for row in [r for r in rows if r['month']=='2026-10']:
 market,acc=row['platform'],row['account'];now=row['values'];prev=next(r['values'] for r in rows if r['month']=='2026-09' and r['account']==acc)
 den='sales' if market=='ozon' else 'own_price' if market=='wb' else 'own';total='itog' if market!='wb' else 'wb_exp'
 components=O.EXP_SVC+['returns','cogs','compensation','other'] if market=='ozon' else ['spp','commission']+W.EXP+['returns_pay','cogs','compensation'] if market=='wb' else Y.MP_EXP+['returns_money','cogs']
 changes=[]
 for key in components:
  a=now.get(key,0)/now[den]*100;b=prev.get(key,0)/prev[den]*100;sign=1 if key in ('compensation','other') and market=='ozon' or key=='compensation' and market=='wb' else -1
  changes.append(dict(key=key,now=now.get(key,0),prev=prev.get(key,0),pct=a,prev_pct=b,impact_pp=sign*(a-b)))
 comparisons.append(dict(platform=market,account=acc,turnover=now[den],net=now['net'],margin=now['margin'],prev_margin=prev['margin'],market_cost_pct=now[total]/now[den]*100,prev_market_cost_pct=prev[total]/prev[den]*100,cogs_pct=now['cogs']/now[den]*100,prev_cogs_pct=prev['cogs']/prev[den]*100,components=sorted(changes,key=lambda x:x['impact_pp'])))
save('comparison.json',comparisons)
for c in comparisons:
 row=next(r for r in rows if r['account']==c['account'] and r['month']=='2026-10')
 print(c['account'],row['last_date'],'turnover',round(c['turnover']),'net',round(c['net']),'margin',round(c['margin'],2),'vs',round(c['prev_margin'],2),'cost%',round(c['market_cost_pct'],2),'vs',round(c['prev_market_cost_pct'],2),'cogs%',round(c['cogs_pct'],2))
 print('drivers',[(d['key'],round(d['impact_pp'],2)) for d in c['components'][:5]])
print('Saved monthly, comparison, source freshness, and Pult news/events.')
