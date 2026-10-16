import sys,json,requests,os
from pathlib import Path
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon import _headers
from collectors.wb import _token,REPORT_URL
out=Path('docs/reports/seasonality_2026-10-04')
for a in ['oz_acc1','oz_acc2']:
 for y in [2024,2025]:
  for m in [8,9]:
   r=requests.post('https://api-seller.ozon.ru/v2/finance/realization',headers=_headers(a),json={'year':y,'month':m},timeout=45)
   d=r.json();rs=(d.get('result') or {}).get('rows') or []
   print(a,y,m,r.status_code,len(rs),flush=True)
   (out/f'{a}_{y}_{m:02}.json').write_text(json.dumps(d,ensure_ascii=False))
for a in ['wb_acc1','wb_acc2']:
 r=requests.get(REPORT_URL,headers={'Authorization':_token(a)},params={'dateFrom':'2025-09-01','dateTo':'2025-09-30','limit':1,'rrdid':0},timeout=(15,45))
 print(a,'2025-09 probe',r.status_code,flush=True)
 (out/f'{a}_probe.json').write_text(json.dumps({'status':r.status_code,'response':r.json() if r.content else None},ensure_ascii=False))
