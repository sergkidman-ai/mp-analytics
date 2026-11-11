# поток: ozon-family-strategy; all writes confined to this stage.
from pathlib import Path
import json,csv,hashlib,datetime,decimal
O=Path(__file__).parent;AUD=O.parent/'ozon_family_audit_20261106';S=AUD/'snapshot';Y=O.parent/'ozon_year_review_20261105'
START='2025-11-05';END='2026-11-04';D=decimal.Decimal
def read(p):return json.loads(p.read_text())
def rows(p):return csv.DictReader(p.open(encoding='utf-8-sig',newline=''))
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str))
def js(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def write(n,data):
 with (O/n).open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
 return h.hexdigest()
def cents(x):return int((D(str(x or '0').replace('\xa0','').replace(' ','').replace(',','.'))*100).quantize(D('1')))
def current():
 cat={str(c['id']):c for c in read(S/'catalog.json')};assert len(cat)==22536 and all(not c['is_archived'] for c in cat.values())
 cards={c['product_id']:c for c in rows(AUD/'cards.csv') if c['product_id'] in cat}
 alias={}
 for pid,c in cat.items():
  for v in [c.get('sku')]+[x.get('sku') for x in c.get('sources',[])]:
   if v:
    k=str(v);assert k not in alias or alias[k]==pid;alias[k]=pid
 assert len(cards)==len(cat)
 return cat,cards,alias
