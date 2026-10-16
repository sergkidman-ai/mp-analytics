import json
from pathlib import Path
from collections import defaultdict
from core.db import query
p=Path('docs/reports/seasonality_2026-10-04');result=[]
for a in ['oz_acc1','oz_acc2']:
 frames={}
 for y in [2025,2026]:
  for m in [8,9]:
   if y==2025:d=json.loads((p/f'{a}_{y}_{m:02}.json').read_text()).get('result') or {}
   else:d=query('select payload from raw_ozon_realization where account=%s and year=%s and month=%s',(a,y,m))[0]['payload']
   f=defaultdict(lambda:{'sales':0,'units':0,'seller':0})
   for row in d.get('rows',[]):
    item=row.get('item') or {};code=item.get('offer_id') or str(item.get('sku'));c=row.get('delivery_commission') or {}
    if not c.get('quantity'):continue
    f[code]['sales']+=float(c.get('amount') or 0);f[code]['units']+=float(c.get('quantity') or 0)
    f[code]['seller']+=float(c.get('amount') or 0)+sum(float(c.get(k) or 0) for k in ['bonus','bank_coinvestment','pick_up_point_coinvestment','stars'])
   frames[(y,m)]=f
 common=set.intersection(*(set(f) for f in frames.values()))
 totals={}
 for k,f in frames.items():totals[f'{k[0]}-{k[1]:02}']={metric:sum(f[c][metric] for c in common) for metric in ['sales','units','seller']}
 r={'account':a,'common_offers':len(common),'totals':totals};result.append(r);print(r)
(p/'ozon_common_offers.json').write_text(json.dumps(result,indent=2))
