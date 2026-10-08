# поток: fin — read-only анализ отчётов
import sys,json,csv,datetime as dt,collections
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,'/opt/mp-analytics')
from core import db
p=Path('/opt/mp-analytics/docs/reports/yandex_weekly_aug_sep_2026')
closure=db.query("SELECT * FROM raw_yandex_closure WHERE ym='2026-08' AND account='ya_acc1'")
sept=p/'september_payments_verified.json'
if not sept.exists():raise RuntimeError('Ждём полного сентябрьского отчёта')
closure+=json.loads(sept.read_text())
services=db.query("SELECT * FROM raw_yandex_services WHERE ym IN ('2026-08','2026-09') AND account='ya_acc1'")
cogs=db.query("""WITH ord AS (SELECT DISTINCT ON (payload->>'id') payload->>'id' oid,(payload->>'creationDate')::date created FROM raw_yandex_stats_order WHERE account='ya_acc1' ORDER BY payload->>'id',loaded_at DESC) SELECT c.*,coalesce(o.created,c.demand_date) basis_date FROM ya_cogs_demand c LEFT JOIN ord o ON o.oid=c.demand_name WHERE c.account='ya_acc1' AND c.status='done' AND coalesce(o.created,c.demand_date) BETWEEN '2026-08-01' AND '2026-09-30'""")
(p/'source_snapshot.json').write_text(json.dumps({'closure':closure,'services':services,'cogs':cogs},default=str,ensure_ascii=False))
weekly=collections.defaultdict(lambda:collections.defaultdict(D));monthly=collections.defaultdict(lambda:collections.defaultdict(D))
def add(date,key,amount):
 date=dt.date.fromisoformat(str(date)[:10]); wk=date-dt.timedelta(days=date.weekday());value=D(str(amount or 0));weekly[wk][key]+=value;monthly[str(date)[:7]][key]+=value
for r in closure:add(r['transaction_date'],'revenue' if r['category']=='revenue' else 'returns',r['amount'])
for r in services:
 add(r['svc_date'],'netting',r['netting']);add(r['svc_date'],'money_cost',r['cost']);add(r['svc_date'],r['category'],D(str(r['cost'] or 0))+D(str(r['netting'] or 0)))
# Агентская строка в Пульте приходит из actual комиссии AGENCY отчёта заказов.
agency_rows=db.query("SELECT (payload->>'creationDate')::date date, sum(coalesce((c->>'actual')::numeric,0)) agency FROM raw_yandex_stats_order r CROSS JOIN LATERAL jsonb_array_elements(r.payload->'commissions') c WHERE r.account='ya_acc1' AND payload->>'creationDate' BETWEEN '2026-08-01' AND '2026-09-30' AND c->>'type'='AGENCY' GROUP BY 1")
(p/'agency_source.json').write_text(json.dumps(agency_rows,default=str,ensure_ascii=False))
for r in agency_rows:
 add(r['date'],'money_cost',r['agency']);add(r['date'],'agency',r['agency'])
for r in cogs:
 assert r['method'] in ('ms_fifo','tovar_fifo','manual'),r['method']
 add(r['basis_date'],'cogs',r['cogs']);add(r['basis_date'],'orders_cogs',1)
fields=['period','turnover','expense','expense_money','cogs','returns','profit','margin','expense_pct','cogs_pct','commission','logistics','advertising','netting','orders_cogs']
def fold(period,v):
 turnover=v['revenue']+v['netting']; expense=v['money_cost']+v['netting'];profit=turnover-expense+v['returns']-v['cogs']
 return {'period':str(period),'turnover':turnover,'expense':expense,'expense_money':v['money_cost'],'cogs':v['cogs'],'returns':-v['returns'],'profit':profit,'margin':profit/turnover*100,'expense_pct':expense/turnover*100,'cogs_pct':v['cogs']/turnover*100,'commission':v['commission'],'logistics':v['logistics'],'advertising':sum(v[k] for k in ['boost_sales','boost_shows','shelf','reviews']),'netting':v['netting'],'orders_cogs':v['orders_cogs']}
out=[fold(k,v) for k,v in sorted(weekly.items())];month=[fold(k,v) for k,v in sorted(monthly.items())]
for name,rs in [('weekly',out),('monthly',month)]:
 (p/(name+'.json')).write_text(json.dumps(rs,default=str,ensure_ascii=False,indent=2))
 with (p/(name+'.csv')).open('w') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rs)
 for r in rs:print(name,{k:(round(v,2) if isinstance(v,D) else v) for k,v in r.items() if k not in ['netting','orders_cogs']})
