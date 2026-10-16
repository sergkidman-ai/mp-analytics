import sys,csv,json
from collections import defaultdict
from pathlib import Path
sys.path.insert(0,'/opt/mp-analytics')
from core.db import query
from collectors.ozon import categorize_operation
out=Path('/opt/mp-analytics/docs/reports/ozon_express_analysis')
posts={(r['account'],r['posting_number']):r for r in query('select account,posting_number,scheme,payload from raw_ozon_posting')}
cost=defaultdict(float)
for r in query("select org,demand_name,cogs from ms_demand_cogs where agent in ('Покупатель Озон','Озон Экспресс')"):
 cost[(r['org'],'-'.join(r['demand_name'].split('-')[:2]))]+=float(r['cogs'] or 0)
org={'oz_acc1':'a5e7ee84-8d63-41f2-9fae-0be2ef90462b','oz_acc2':'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
ops=query("select account,payload from raw_ozon_transaction where (payload->>'operation_date')::date between '2026-06-01' and '2026-10-04'")
g=defaultdict(lambda:defaultdict(float)); first={}; types=defaultdict(lambda:defaultdict(float))
for r in ops:
 a=r['account'];p=r['payload'];pn=(p.get('posting') or {}).get('posting_number') or '';k=(a,pn)
 if not pn:continue
 c=categorize_operation(p)
 for t,v in c.items():g[k][t]+=v
 g[k]['amount']+=float(p.get('amount') or 0)
 types[k][p.get('operation_type_name') or p.get('operation_type')]+=float(p.get('amount') or 0)
 if float(p.get('accruals_for_sale') or 0)>0:first[k]=min(first.get(k,'9999'),p['operation_date'][:10])
# Укороченный номер объединяет несколько отправлений: распределяем только эквайринг
# по положительной выручке всех отправлений этого заказа, включая вне когорты.
children=defaultdict(list)
for k in first:
 children[(k[0], '-'.join(k[1].split('-')[:2]))].append(k)
allocation=defaultdict(float)
for r in ops:
 p=r['payload'];pn=(p.get('posting') or {}).get('posting_number') or ''
 if 'acquiring' not in (p.get('operation_type') or '').lower() and 'эквайринг' not in (p.get('operation_type_name') or '').lower():continue
 k=(r['account'],pn)
 if k in first:continue  # точное отправление уже учтено выше
 targets=children.get(k,[])
 total=sum(g[x]['revenue'] for x in targets)
 if not total:continue
 amount=float(p.get('amount') or 0)
 shares=[amount*g[x]['revenue']/total for x in targets]
 shares[-1]=amount-sum(shares[:-1])
 assert abs(sum(shares)-amount)<1e-8
 for x,v in zip(targets,shares):allocation[x]+=v
for k,v in allocation.items():
 g[k]['amount']+=v;g[k]['acquiring']+=v
rows=[];summ=defaultdict(lambda:defaultdict(float))
for k,day in first.items():
 if not '2026-06-01'<=day<='2026-09-30':continue
 a,pn=k;p=posts.get(k);d=g[k]
 if not p:mode='unknown'
 elif p['payload'].get('is_express') is True:mode='express'
 elif p['scheme']=='fbs' and p['payload'].get('tpl_integration_type')=='ozon':mode='FBS'
 else:mode='other'
 c=cost.get((org[a],'-'.join(pn.split('-')[:2])))
 ret=d['returns']<0
 row=dict(account=a,posting=pn,first_sale=day,mode=mode,revenue=d['revenue'],returns=d['returns'],commission=d['commission'],amount=d['amount'],cogs=c,profit=None if c is None else d['amount']-c,has_return=ret,**{t:d[t] for t in ['logistics','partners','acquiring','advertising','subscription','penalties','other']})
 rows.append(row)
 s=summ[(a,mode)];s['orders']+=1;s['revenue']+=d['revenue'];s['amount']+=d['amount'];s['returns_orders']+=ret
 if c is not None and not ret:
  s['matched_no_return']+=1;s['matched_revenue']+=d['revenue'];s['profit']+=d['amount']-c;s['cogs']+=c;s['commission']+=d['commission'];s['acquiring']+=d['acquiring'];s['loss_orders']+=(d['amount']-c)<0
with (out/'orders.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
result=[dict(account=k[0],mode=k[1],**v,margin=v['profit']/v['matched_revenue']*100 if v['matched_revenue'] else None,profit_per_order=v['profit']/v['matched_no_return'] if v['matched_no_return'] else None) for k,v in summ.items()]
(out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
monthly=defaultdict(lambda:defaultdict(float))
for r in rows:
 s=monthly[(r['first_sale'][:7],r['account'],r['mode'])]
 s['orders']+=1;s['returns_orders']+=r['has_return'];s['missing_cogs']+=r['cogs'] is None
 if r['cogs'] is None or r['has_return']:continue
 s['matched_no_return']+=1;s['matched_revenue']+=r['revenue'];s['profit']+=r['profit'];s['acquiring']+=r['acquiring'];s['loss_orders']+=r['profit']<0
monthly_result=[dict(month=k[0],account=k[1],mode=k[2],**v,margin=v['profit']/v['matched_revenue']*100 if v['matched_revenue'] else None,profit_per_order=v['profit']/v['matched_no_return'] if v['matched_no_return'] else None) for k,v in sorted(monthly.items())]
(out/'monthly.json').write_text(json.dumps(monthly_result,ensure_ascii=False,indent=2))

for r in result:print(json.dumps(r,ensure_ascii=False))
