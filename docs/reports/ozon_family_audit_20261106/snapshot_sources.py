# поток: ozon-family. File-only read snapshot; no collectors/main/upserts.
import sys, json, pathlib, datetime, time, hashlib
ROOT=pathlib.Path('/opt/mp-analytics'); sys.path.insert(0,str(ROOT))
from core import db
from collectors.ozon import _headers, PRODUCT_LIST_URL, PRODUCT_INFO_URL
import psycopg2, psycopg2.extras, requests
OUT=pathlib.Path(__file__).parent/'snapshot'
def stamp(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(name,obj): (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,default=str))
def db_snapshot():
    conn=psycopg2.connect(db.DATABASE_URL)
    conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
    queries={
      'set_cost':'SELECT external_code,components,n_components,resolved_at,updated_at FROM set_cost',
      'prc_tc_model':'SELECT external_code,title,additional_title,united_title,similar_title,consumable_type,color,resource,chip,brand,printer_models,raw,first_seen,last_seen,gone_at FROM prc_tc_model',
      'prc_tc_link':'SELECT * FROM prc_tc_link', 'prc_tc_code':'SELECT * FROM prc_tc_code',
      'ms_product':'SELECT ms_id,name,article,code,external_code,archived,updated_at FROM ms_product',
      'supplier_stock':'SELECT captured_at,ms_id,store,name,article,external_code,supplier,stock,reserve FROM supplier_stock',
      'ozon_product':"SELECT * FROM ozon_product WHERE account='oz_acc1'",
      'raw_ozon_attributes':"SELECT * FROM raw_ozon_attributes WHERE account='oz_acc1'"}
    meta={'started_at':stamp(),'isolation':'REPEATABLE READ READ ONLY','queries':{}}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
      for name,q in queries.items():
        c.execute(q); rows=[dict(r) for r in c.fetchall()]; save('db_'+name+'.json',rows)
        meta['queries'][name]={'sql':q,'rows':len(rows),'file':'db_'+name+'.json'}
        print('DB',name,len(rows),flush=True)
    conn.rollback(); conn.close(); meta['finished_at']=stamp(); save('db_manifest.json',meta)
def api_snapshot():
    session=requests.Session(); session.headers.update(_headers('oz_acc1'))
    log=[]
    def read(url,body,name):
      p=OUT/name
      if p.exists(): return json.loads(p.read_text())
      start=stamp()
      for attempt in range(3):
        r=session.post(url,json=body,timeout=120)
        if r.status_code!=429: break
        time.sleep(min(30,int(r.headers.get('Retry-After','5'))+1))
      if r.status_code!=200:
        save(name,{'started_at':start,'observed_at':stamp(),'http':r.status_code,'request':body,'error':r.text[:1500]})
        raise RuntimeError('read endpoint failed HTTP '+str(r.status_code))
      obj={'started_at':start,'observed_at':stamp(),'http':r.status_code,'endpoint':url,'request':body,'body':r.json()}
      save(name,obj); time.sleep(.2); return obj
    archive = sys.argv[-1]=='archive'
    visibility='ARCHIVED' if archive else 'ALL'
    prefix='archived' if archive else 'all'
    items=[]; seen=set(); last=''; pages=[]
    for page in range(100):
      z=read(PRODUCT_LIST_URL,{'filter':{'visibility':visibility},'last_id':last,'limit':1000},f'list_{prefix}_{page:03}.json')
      res=z['body']['result']; chunk=res.get('items',[]); items+=chunk
      pages.append({'file':f'list_{prefix}_{page:03}.json','rows':len(chunk),'total':res.get('total'),'last_id':res.get('last_id'),'at':z['observed_at']})
      nxt=res.get('last_id','')
      if len(chunk)<1000: break
      if not nxt or nxt in seen: raise RuntimeError('pagination cycle/missing cursor')
      seen.add(nxt); last=nxt
    else: raise RuntimeError('page cap')
    list_final_short=len(chunk)<1000
    pids=sorted({str(x['product_id']) for x in items},key=int)
    cards=[]; provenance=[]
    for i in range(0,len(pids),1000):
      name=f'info_archived_{i//1000:03}.json' if archive else f'info_{i//1000:03}.json'
      z=read(PRODUCT_INFO_URL,{'product_id':pids[i:i+1000]},name)
      chunk=z['body'].get('items',[]); cards+=chunk
      provenance += [{'product_id':str(x['id']),'source':'snapshot/'+name,'observed_at':z['observed_at']} for x in chunk]
      print('API info',i//1000,len(chunk),flush=True)
    got={str(x['id']) for x in cards}
    save('catalog_archived.json' if archive else 'catalog.json',cards); save('catalog_archived_provenance.json' if archive else 'catalog_provenance.json',provenance)
    save('catalog_archived_coverage.json' if archive else 'catalog_coverage.json',{'account':'oz_acc1','visibility':visibility,'pages':pages,'listed_rows':len(items),'unique_listed':len(pids),'info_rows':len(cards),'unique_info':len(got),'missing_info':sorted(set(pids)-got),'unexpected_info':sorted(got-set(pids)),'list_duplicates':len(items)-len(pids),'info_duplicates':len(cards)-len(got),'final_page_short':list_final_short,'completed_at':stamp()})
    print('CATALOG',len(pids),len(got),'missing',len(set(pids)-got),flush=True)
if __name__=='__main__':
    if sys.argv[-1]=='db': db_snapshot()
    elif sys.argv[-1] in ('api','archive'): api_snapshot()
