# поток: mkt
"""Targeted read-only supplements for missing SKU metadata and sales."""
import sys,pathlib,json,datetime as dt,time,collections
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors.ozon import _headers,PRODUCT_INFO_URL
from core import db
O=pathlib.Path(__file__).parent;R=O/'revision_raw';R.mkdir(exist_ok=True)
TARGET='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split()
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()
H=_headers('oz_acc1')
# These identities, article names, availability and family metadata were missing from the original export.
ids=TARGET+['6201364326']
p=R/'product_info_targets_candidate.json'
if not p.exists():
 r=requests.post(PRODUCT_INFO_URL,headers=H,json={'sku':ids},timeout=90)
 save('revision_raw/'+p.name,{'http':r.status_code,'retrieved_at_utc':stamp(),'purpose':'Missing offer_id/name/stocks/model_info for fixed 20 SKU and actual candidate SKU','body':r.json()})
z=json.loads(p.read_text());its=z['body'].get('items',[])
print('Product info HTTP',z['http'],'items',len(its),'fields',list(its[0]) if its else [],flush=True)
print('Model metadata fields:',list(its[0].get('model_info',{})) if its else [],flush=True)
with db.get_conn() as cn:
 cn.set_session(readonly=True)
 with cn.cursor() as c:
  c.execute("SELECT min(period_from),max(period_to),count(*) FROM sales WHERE platform='ozon' AND account='oz_acc1'")
  result=c.fetchone()
  save('revision_db_sales_coverage.json',{'account':'oz_acc1','min_period_from':result[0],'max_period_to':result[1],'rows':result[2],'query':'SELECT min(period_from),max(period_to),count(*) FROM sales WHERE platform=ozon AND account=oz_acc1','retrieved_at_utc':stamp()})
print('DB sales coverage',result,flush=True)
