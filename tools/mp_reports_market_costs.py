# поток: fin
"""Restore actual Market shipment FIFO in staging, with bounded read-only MS calls."""
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from types import SimpleNamespace

from psycopg2.extensions import parse_dsn, make_dsn
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db, ms_api

ROOT=Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))/'market_costs'


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        params=parse_dsn(db.DATABASE_URL);params['dbname']='bi_history_20261104'
        db.DATABASE_URL=make_dsn(**params);os.environ['DATABASE_URL']=db.DATABASE_URL
        assert db.query('SELECT current_database() name')[0]['name']=='bi_history_20261104'
        ms_api.TIMEOUT=30;ms_api.RETRIES=3
        from collectors import ya_cogs_demand as ya, ms_demand_cogs as demands
        from tools.bi_history_restore import narrow_ms_read
        orgs=ms_api.get('/entity/organization',{'filter':'name=ООО "ЦИФРОВОЙ КВАДРАТ"','limit':20})['rows']
        if len(orgs)!=1:raise RuntimeError('Market organization ambiguous')
        org=orgs[0]['id'];org_href=orgs[0]['meta']['href']
        agent_names={}
        for agent_name in ya.AGENTS:
            for row in ms_api.get('/entity/counterparty',{'filter':'name='+agent_name,'limit':20}).get('rows',[]):agent_names[row['id']]=agent_name
        stamp={'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'calls':0,'demands':0,'partial':0}
        last=[0.0]
        pace_lock=threading.Lock()
        def save():
            stamp['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
            tmp=ROOT/'state.tmp';tmp.write_text(json.dumps(stamp,ensure_ascii=False,indent=2));tmp.replace(ROOT/'state.json')
        def api(path,params=None):
            with pace_lock:
                wait=.25-(time.monotonic()-last[0])
                if wait>0:time.sleep(wait)
                last[0]=time.monotonic();stamp['calls']+=1
            return ms_api.get(path,params)
        def narrow(path,_tries=0):
            return narrow_ms_read(lambda p,t:api(p),path,(dt.date.today()+dt.timedelta(days=1)).isoformat())
        demands.get=narrow
        original_byoperation=demands.byoperation_cogs
        original_request=ya.requests.get
        def requests_get(url,**kwargs):
            if not url.startswith(ya.MS):return original_request(url,**kwargs)
            params=dict(kwargs.get('params')or{})
            if url.endswith(('/entity/demand','/entity/salesreturn')):
                params['filter']=params.get('filter','')+';organization='+org_href
                if url.endswith('/entity/demand'):params['order']='moment,desc'
            if params:
                url += ('&'if'?'in url else'?')+urlencode(params)
            parts=urlsplit(url);query=parse_qs(parts.query)
            if parts.path.endswith(('/entity/counterparty','/entity/organization')):
                query['limit']=['20'];url=urlunsplit(parts._replace(query=urlencode(query,doseq=True)))
            relative=url.split(ms_api.BASE,1)[-1]
            return SimpleNamespace(json=lambda:narrow(relative))
        ya.requests.get=requests_get
        # Missing FIFO remains missing; current card prices cannot prove historical costs.
        ya._cost_seb_map=lambda:{}
        ya.fifo_fallback.load=lambda:SimpleNamespace(impute=lambda *args:(0.0,'need_manual'))
        history=json.loads(Path('/opt/mp-analytics/reports/data/mp_yandex_hist.json').read_text())
        closed={month+'-01' for month in history.get('period_keys',[])}
        ya._frozen_set=lambda:closed
        cache={r['demand_id']:r for r in db.query("SELECT * FROM ms_demand_cogs WHERE org=%s AND agent=ANY(%s)",(org,list(ya.AGENTS)))}
        positions={}
        for row in db.query("SELECT p.* FROM ms_demand_pos p JOIN ms_demand_cogs d USING(demand_id) WHERE d.org=%s AND d.agent=ANY(%s)",(org,list(ya.AGENTS))):
            positions.setdefault(row['demand_id'],[]).append({'ms_id':row['ms_id'],'cost':float(row['cost']or 0),'qty':float(row['qty']or 0)})
        metadata={}
        pending={}
        pool=concurrent.futures.ThreadPoolExecutor(max_workers=3)
        def requests_tracking(url,**kwargs):
            response=requests_get(url,**kwargs)
            value=response.json()
            if url.endswith('/entity/demand'):
                stamp['last_list_total']=value.get('meta',{}).get('size')
                for item in value.get('rows',[]):
                    metadata[item['id']]=item
                    actual_agent=agent_names.get(ms_api.meta_id(item,'agent'))
                    if actual_agent and item['id'] in cache and cache[item['id']]['agent']!=actual_agent:
                        db.execute('UPDATE ms_demand_cogs SET agent=%s WHERE demand_id=%s',(actual_agent,item['id']))
                for item in value.get('rows',[]):pending[item['id']]=pool.submit(fetch_actual,item['id'])
            return SimpleNamespace(json=lambda:value)
        ya.requests.get=requests_tracking
        def fetch_actual(uid):
            row=cache.get(uid);pos=positions.get(uid,[])
            if row and float(row['cogs']or 0)>0 and pos and all(x['cost']>0 for x in pos):
                cost,qty=float(row['cogs']),float(row['qty']or 0)
            else:
                cost,qty,pos=original_byoperation(uid)
                item=metadata.get(uid)
                complete=bool(pos) and abs(sum(x['qty']for x in pos)-qty)<.00001 and all(x['cost']>0 for x in pos if x['qty']>0)
                if not complete:cost=0.0;stamp['partial']+=1
                if item:
                    rec={'demand_id':uid,'demand_name':item['name'],'org':org,'agent':agent_names[ms_api.meta_id(item,'agent')],'moment':item['moment'],'cogs':cost,'qty':qty,'npos':len(pos)}
                    db.upsert('ms_demand_cogs',[rec],['demand_id'])
                    if pos:db.upsert('ms_demand_pos',[{'demand_id':uid,**x}for x in pos if x['ms_id']],['demand_id','ms_id'])
            return cost,qty,pos
        def actual_fifo(uid):
            cost,qty,pos=pending[uid].result() if uid in pending else fetch_actual(uid)
            stamp['demands']+=1
            if stamp['demands']%25==0:
                save();print(json.dumps(stamp,ensure_ascii=False),flush=True)
            return cost,qty,pos
        ya.MDC.byoperation_cogs=actual_fifo
        save()
        as_of=dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()))
        since=sys.argv[1] if len(sys.argv)>1 else (as_of.replace(day=1)-dt.timedelta(days=1)).replace(day=1).isoformat()
        while since[:7] in {x[:7] for x in closed}:
            since=(dt.date.fromisoformat(since)+dt.timedelta(days=32)).replace(day=1).isoformat()
        if since>as_of.isoformat():raise RuntimeError('No open Market month to refresh')
        count=ya.collect(since)
        pool.shutdown(wait=True)
        stamp.update(status='complete',rows=count);save()
        print(json.dumps(stamp,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
