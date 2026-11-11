# поток: mkt
import sys,pathlib,json,datetime as dt,time
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from collectors import ozon_ads as a,ozon_bids as b
O=pathlib.Path(__file__).parent;R=O/'revision_raw';H={'Authorization':'Bearer '+a._token('oz_acc1')}
for x in json.loads((O/'revised_existing_campaign_overlaps.json').read_text()):
 cid=x['campaign_id'];p=R/f'products_refresh_{cid}.json'
 if p.exists():continue
 ps,code=b._campaign_products(H,cid)
 p.write_text(json.dumps({'http':code,'retrieved_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'purpose':'justified additional candidate: overlap with prepared SKU set','products':ps},ensure_ascii=False,indent=2)+'\n')
 print('Overlap candidate',cid,'HTTP',code,'products',len(ps));time.sleep(.3)
