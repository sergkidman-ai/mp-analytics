# поток: mkt / ozon
"""Bounded read-only DATE reports; two active jobs, saved UUIDs, no duplicate requests."""
import datetime as dt
import io
import json
import pathlib
import sys
import time
import zipfile
import argparse

sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors.ozon_ads import _token,PERF

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=pathlib.Path,default=pathlib.Path(__file__).parent)
parser.add_argument('--jobs',type=pathlib.Path,help='Saved bounded report jobs; defaults to output/sku_report_jobs.json')
args=parser.parse_args()
OUT=args.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
RAW=OUT/'sku_reports';RAW.mkdir(exist_ok=True)
jobs=json.loads((args.jobs or OUT/'sku_report_jobs.json').read_text())['jobs']
original=requests.sessions.Session.request
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def guard(self,method,url,**kw):
    method=method.upper()
    assert (method=='POST' and url in [PERF+'/api/client/token',PERF+'/api/client/statistics']) or (method=='GET' and url.startswith(PERF+'/api/client/statistics/'))
    r=original(self,method,url,**kw)
    log={'at_utc':now(),'method':method,'path':url[len(PERF):],'http':r.status_code}
    if not url.endswith('/token'):log.update(body=kw.get('json'),params=kw.get('params'))
    with (OUT/'sku_report_reads.jsonl').open('a') as f:f.write(json.dumps(log,ensure_ascii=False)+'\n')
    return r
requests.sessions.Session.request=guard
h={'Authorization':'Bearer '+_token('oz_acc1')};pending=[];active={};results=[]
for job in jobs:
    dest=RAW/job['key'];dest.mkdir(exist_ok=True)
    done=dest/'complete.json';req=dest/'request.json'
    if done.exists():results.append(json.loads(done.read_text()));continue
    if req.exists():
        z=json.loads(req.read_text());active[job['key']]={'job':job,'uuid':z['response']['UUID'],'started':time.monotonic()}
    else:pending.append(job)
timed_out=False
while pending or active:
    while pending and len(active)<2:
        job=pending[0];r=requests.post(PERF+'/api/client/statistics',headers=h,json=job['request'],timeout=120)
        if r.status_code==429:
            print('SKU report quota/rate limit; preserve queue',flush=True);time.sleep(15);break
        r.raise_for_status();z={'requested_at_utc':now(),'request':job['request'],'http':r.status_code,'response':r.json()}
        (RAW/job['key']/'request.json').write_text(json.dumps(z,ensure_ascii=False,indent=2)+'\n')
        active[job['key']]={'job':job,'uuid':z['response']['UUID'],'started':time.monotonic()};pending.pop(0)
        print('SKU report requested',job['key'],'campaigns',len(job['request']['campaigns']),flush=True)
    for key,entry in list(active.items()):
        dest=RAW/key;r=requests.get(PERF+'/api/client/statistics/'+entry['uuid'],headers=h,timeout=60);r.raise_for_status();status=r.json()
        (dest/'status.json').write_text(json.dumps({'checked_at_utc':now(),'http':r.status_code,'body':status},ensure_ascii=False,indent=2)+'\n')
        state=status.get('state')
        if state=='OK':
            r=requests.get(PERF+'/api/client/statistics/report',headers=h,params={'UUID':entry['uuid']},timeout=180);r.raise_for_status()
            names=[]
            if r.content[:2]==b'PK':
                (dest/'report.zip').write_bytes(r.content)
                with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
                    for name in archive.namelist():
                        if not name.endswith('/'):
                            safe=pathlib.Path(name).name;(dest/safe).write_bytes(archive.read(name));names.append(safe)
            else:(dest/'report.csv').write_bytes(r.content);names=['report.csv']
            done={'key':key,'uuid':entry['uuid'],'downloaded_at_utc':now(),'request':entry['job']['request'],'http':r.status_code,'files':names}
            (dest/'complete.json').write_text(json.dumps(done,ensure_ascii=False,indent=2)+'\n');results.append(done);del active[key]
            print('SKU report ready',key,'files',len(names),flush=True)
        elif state in ['ERROR','CANCELLED']:
            results.append({'key':key,'state':state});del active[key];print('SKU report terminal',key,state,flush=True)
        elif time.monotonic()-entry['started']>1800:
            timed_out=True;print('SKU report still queued after 30 min; UUID preserved',key,flush=True);break
    (OUT/'sku_collection_status.json').write_text(json.dumps({'checked_at_utc':now(),'jobs_expected':len(jobs),'complete':sum(x.get('http')==200 for x in results),'active_jobs':list(active),'pending_jobs':[x['key'] for x in pending],'results':results},ensure_ascii=False,indent=2)+'\n')
    if timed_out:break
    if pending or active:time.sleep(5)
print('SKU HISTORY REPORT COLLECTION', 'INCOMPLETE' if active or pending else 'FINISHED', 'downloads',sum(x.get('http')==200 for x in results),flush=True)
