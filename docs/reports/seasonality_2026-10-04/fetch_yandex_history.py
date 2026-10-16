import sys,json
from pathlib import Path
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon import _headers
_headers('oz_acc1')
from collectors.yandex_closure import _cfg,_closure_csvs,_parse_csvs
key,camps=_cfg();cid='148691041'
out=Path('docs/reports/seasonality_2026-10-04')
for ym in ['2025-08','2025-09','2024-08','2024-09']:
 try:
  sheets=_closure_csvs(key,cid,int(ym[:4]),int(ym[5:]),timeout=180)
  rows=_parse_csvs(sheets,'ya_acc1',ym,cid)
  (out/f'ya_{ym}.json').write_text(json.dumps(rows,ensure_ascii=False,default=str))
  print(ym,len(rows),sum(float(r['amount']) for r in rows if r['category']=='revenue'),flush=True)
 except Exception as e:print(ym,type(e).__name__,str(e)[:180],flush=True)
