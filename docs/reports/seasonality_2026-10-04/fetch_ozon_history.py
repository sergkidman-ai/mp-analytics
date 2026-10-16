import sys,json,requests
from pathlib import Path
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon import _headers
out=Path('docs/reports/seasonality_2026-10-04')
for a in ['oz_acc1','oz_acc2']:
 for y in [2024,2025]:
  for m in [6,7,10,11,12]:
   if y==2024 and a=='oz_acc2':continue
   r=requests.post('https://api-seller.ozon.ru/v2/finance/realization',headers=_headers(a),json={'year':y,'month':m},timeout=45)
   d=r.json();rs=(d.get('result') or {}).get('rows') or []
   print(a,y,m,r.status_code,len(rs),flush=True)
   (out/f'{a}_{y}_{m:02}.json').write_text(json.dumps(d,ensure_ascii=False))
