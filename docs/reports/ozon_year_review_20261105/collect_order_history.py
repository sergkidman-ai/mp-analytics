# поток: mkt / ozon
"""Fill only missing annual FBS/FBO windows with the existing verified read-only posting client."""
import datetime as dt
import json
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.ozon_postings import fetch_postings, FBS_URL, FBO_URL

OUT = pathlib.Path(__file__).parent
RAW = OUT / 'raw'
recovery = OUT.parent / 'ozon_recovery_20261103'
start, end = dt.date(2025,11,5), dt.date(2026,11,4)
cache = []
for scheme in ['fbs','fbo']:
    for path, first, last in [
        (recovery/f'revision_raw/postings_baseline_{scheme}.json','2026-10-02','2026-10-29'),
        (recovery/f'daily_20261105/raw/monitor_postings_20261031_20261104_{scheme}.json','2026-10-31','2026-11-04')]:
        z=json.loads(path.read_text())
        if z.get('complete'):
            cache.append({'scheme':scheme,'from_msk':first,'through_msk':last,'path':str(path.resolve()),'kind':'reused_complete_snapshot'})
for path,day in [(RAW/'oldest_postings.json','2025-11-05'),(RAW/'spring_postings.json','2026-05-05')]:
    if path.exists() and json.loads(path.read_text()).get('complete'):
        cache.append({'scheme':'fbs','from_msk':day,'through_msk':day,'path':str(path.resolve()),'kind':'reused_complete_probe'})

original=requests.sessions.Session.request
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def guard(self,method,url,**kw):
    assert method.upper()=='POST' and url in [FBS_URL,FBO_URL], 'Only posting list reads allowed'
    r=original(self,method,url,**kw)
    with (OUT/'order_read_requests.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc':now(),'method':'POST','path':url.split('.ru',1)[-1],'http':r.status_code,'body':kw.get('json')},ensure_ascii=False)+'\n')
    return r
requests.sessions.Session.request=guard
coverage=list(cache)
for scheme in ['fbs','fbo']:
    covered=set()
    for x in cache:
        if x['scheme']!=scheme:continue
        a,b=dt.date.fromisoformat(x['from_msk']),dt.date.fromisoformat(x['through_msk'])
        covered.update(a+dt.timedelta(days=i) for i in range((b-a).days+1))
    d=start
    while d<=end:
        if d in covered:d+=dt.timedelta(days=1);continue
        stop=d
        while stop<end and (stop-d).days<6 and stop+dt.timedelta(days=1) not in covered:stop+=dt.timedelta(days=1)
        path=RAW/f'postings_{scheme}_{d}_{stop}.json'
        if path.exists() and json.loads(path.read_text()).get('complete'):
            z=json.loads(path.read_text())
        else:
            try:
                rows=fetch_postings('oz_acc1',scheme,d.isoformat()+'T00:00:00+03:00',stop.isoformat()+'T23:59:59.999000+03:00',limit=100,max_pages=200,window_budget=180,lock_timeout=2)
                z={'account':'oz_acc1','scheme':scheme,'from_msk':d.isoformat(),'through_msk':stop.isoformat(),'retrieved_at_utc':now(),'complete':True,'postings':rows}
            except Exception as e:
                z={'account':'oz_acc1','scheme':scheme,'from_msk':d.isoformat(),'through_msk':stop.isoformat(),'retrieved_at_utc':now(),'complete':False,'error_type':type(e).__name__}
            path.write_text(json.dumps(z,ensure_ascii=False,indent=2)+'\n')
        coverage.append({'scheme':scheme,'from_msk':d.isoformat(),'through_msk':stop.isoformat(),'path':str(path.resolve()),'kind':'new_missing_window','complete':z.get('complete'),'posting_count':len(z.get('postings',[]))})
        (OUT/'order_collection_coverage.json').write_text(json.dumps({'account':'oz_acc1','requested_year':[str(start),str(end)],'windows':coverage,'finished':False,'database_data_writes':0},ensure_ascii=False,indent=2)+'\n')
        print('Annual orders',scheme,d,stop,'complete',z.get('complete'),'rows',len(z.get('postings',[])),z.get('error_type',''),flush=True)
        d=stop+dt.timedelta(days=1)
(OUT/'order_collection_coverage.json').write_text(json.dumps({'account':'oz_acc1','requested_year':[str(start),str(end)],'windows':coverage,'finished':True,'database_data_writes':0,'marketplace_mutations':0},ensure_ascii=False,indent=2)+'\n')
print('ANNUAL ORDER WINDOW COLLECTION FINISHED',flush=True)
