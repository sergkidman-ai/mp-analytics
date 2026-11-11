# поток: fin
"""Resume historical source reads in an isolated DB; never publish financial marts."""
import calendar
import concurrent.futures
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import http.client
import ssl
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn
from psycopg2.extras import Json
import requests

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from core import db

ROOT = Path('/root/bi-recovery-audit/history')
STAGE = 'bi_history_20261104'
TODAY = dt.date.today()
END = TODAY - dt.timedelta(days=1)
if (ROOT/'initialized.json').exists():
    END=dt.date.fromisoformat(json.loads((ROOT/'initialized.json').read_text())['source_until'])
AS_OF=END+dt.timedelta(days=1)
MONTHS = ['2026-10', '2026-11'] + [f'2026-{m:02d}' for m in range(9, 0, -1)]
MONTHS = [m for m in MONTHS if m <= END.strftime('%Y-%m')]
STATE_LOCK = threading.RLock()
STATE = {}
LIVE_DSN = db.DATABASE_URL
PARAMS = parse_dsn(LIVE_DSN)
with psycopg2.connect(LIVE_DSN) as _c:
    PARAMS.setdefault('user', _c.get_dsn_parameters()['user'])
STAGE_DSN = make_dsn(**{**PARAMS, 'dbname': STAGE})


def emit(event, **fields):
    print(json.dumps({'time':dt.datetime.now(dt.timezone.utc).isoformat(),
                      'event':event, **fields}, ensure_ascii=False), flush=True)


def save():
    with STATE_LOCK:
        STATE['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        p = ROOT/'checkpoint.json.tmp'
        p.write_text(json.dumps(STATE, ensure_ascii=False, indent=2))
        p.chmod(0o600)
        p.replace(ROOT/'checkpoint.json')


def mark(key, **result):
    with STATE_LOCK:
        STATE.setdefault('tasks', {})[key] = result
        save()
    emit('task', key=key, **result)


def completed(key):
    with STATE_LOCK:
        value=STATE.get('tasks',{}).get(key,{})
        return value.get('status') == 'done' or (value.get('status')=='unavailable' and value.get('http')==403)


def space_check():
    if shutil.disk_usage(ROOT).free < 3*1024**3:
        raise RuntimeError('disk_reserve_reached')


def bounds(month):
    y,m=map(int,month.split('-'))
    first=dt.date(y,m,1)
    last=min(dt.date(y,m,calendar.monthrange(y,m)[1]),END)
    return first.isoformat(),last.isoformat()


def pg_env(params):
    env=os.environ.copy()
    names={'dbname':'DATABASE','user':'USER','password':'PASSWORD','host':'HOST','port':'PORT','sslmode':'SSLMODE'}
    for k,v in params.items(): env['PG'+names.get(k,k.upper())]=v
    return env


def initialize():
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    exists=subprocess.check_output(['runuser','-u','postgres','--','psql','-Atc',
            "SELECT 1 FROM pg_database WHERE datname='"+STAGE+"'"],text=True).strip()
    if exists and (ROOT/'initialized.json').exists():
        emit('stage_already_initialized');return
    if not exists:
        subprocess.run(['runuser','-u','postgres','--','createdb','--owner',PARAMS['user'],STAGE],check=True)
    schema=ROOT/'public_schema.sql'
    subprocess.run(['pg_dump','--schema-only','--schema=public','--no-owner','--no-privileges','--file',str(schema)],
                   env=pg_env(PARAMS),check=True,stdout=subprocess.DEVNULL)
    schema.write_text(schema.read_text().replace('CREATE SCHEMA public;', 'CREATE SCHEMA IF NOT EXISTS public;'))
    schema.chmod(0o600)
    result=subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-f',str(schema)],
              env=pg_env({**PARAMS,'dbname':STAGE}),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    if result.returncode:
        (ROOT/'schema_error.log').write_text(result.stderr)
        raise RuntimeError('stage_schema_initialization_failed')
    copies={}
    with psycopg2.connect(LIVE_DSN) as live, psycopg2.connect(STAGE_DSN) as stage:
        for table in ['products','ms_product','supplier_stock','set_cost','tc_buy_price','wb_cards','wb_price']:
            with tempfile.TemporaryFile(mode='w+b') as f,live.cursor() as q,stage.cursor() as w:
                q.copy_expert(sql.SQL('COPY public.{} TO STDOUT WITH BINARY').format(sql.Identifier(table)).as_string(live),f)
                f.seek(0)
                w.copy_expert(sql.SQL('COPY public.{} FROM STDIN WITH BINARY').format(sql.Identifier(table)).as_string(stage),f)
                w.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(table)))
                copies[table]=w.fetchone()[0]
            stage.commit()
    data={'database':STAGE,'source_since':'2026-01-01','source_until':END.isoformat(),
          'priority_months':MONTHS,'reference_rows':copies,'production_data_changed':False}
    (ROOT/'initialized.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    emit('initialized',**data)


def wb_lane(account):
    from collectors import wb
    wb.MAX_429=2
    keyname='WB_TOKEN_FINANCE_'+('ACC1' if account=='wb_acc1'else'ACC2')
    if not os.getenv(keyname):
        try: wb._finance_token(account)
        except RuntimeError:
            # This existing token successfully passed the Finance API read probe.
            os.environ[keyname]=wb._token(account)
    for month in MONTHS:
        key=f'wb/{account}/{month}'
        if completed(key):continue
        space_check();first,last=bounds(month)
        try:
            rows=wb.fetch_report(account,first,last,limit=25000)
            n=wb.load_raw(account,rows,first,last)
            mark(key,status='done',rows=len(rows),saved=n,first=first,last=last)
        except Exception as e:
            code=getattr(getattr(e,'response',None),'status_code',None)
            mark(key,status='failed',error=type(e).__name__,http=code)
            if code in (401,403):break


def ozon_lane(account):
    from collectors import ozon_accrual as accrual,ozon_realization as realization
    for month in MONTHS:
        first,last=bounds(month);day=dt.date.fromisoformat(first);end=dt.date.fromisoformat(last)
        while day<=end:
            key=f'ozon/{account}/{day.isoformat()}'
            if completed(key):
                day+=dt.timedelta(days=1)
                continue
            if not completed(key):
                space_check()
                try:
                    rows=accrual.fetch_day(account,day.isoformat(),max_pages=100,window_budget=60)
                    n=accrual.load_raw(account,rows)
                    mark(key,status='done',rows=len(rows),saved=n)
                except Exception as e:
                    code=getattr(getattr(e,'response',None),'status_code',None)
                    mark(key,status='failed',error=type(e).__name__,http=code)
                    if code in (401,403):return
            day+=dt.timedelta(days=1)
            time.sleep(.25)
        key=f'ozon_realization/{account}/{month}'
        if not completed(key) and month<AS_OF.strftime('%Y-%m'):
            try:
                y,m=map(int,month.split('-'));value=realization.fetch(account,y,m)
                n=realization.load_raw(account,y,m,value)
                mark(key,status='done' if value is not None else 'unavailable',
                     rows=len((value or {}).get('rows')or[]),saved=n)
            except Exception as e:
                mark(key,status='failed',error=type(e).__name__)


def yandex_lane():
    from collectors import yandex_monthly as ym
    h={'Api-Key':os.getenv('YANDEX_API_KEY_ACC1',''),'Content-Type':'application/json'}
    response=requests.get(ym.API+'/campaigns',headers=h,params={'page':1,'pageSize':50},timeout=(10,45))
    response.raise_for_status()
    campaigns=response.json().get('campaigns',[])
    if len(campaigns)>=50:raise RuntimeError('campaign_discovery_requires_pagination')
    business=os.getenv('YANDEX_BUSINESS_ID_ACC1')
    campaigns=[c for c in campaigns if not(c.get('business')or{}).get('id') or str(c['business']['id'])==str(business)]
    emit('yandex_campaigns_discovered',count=len(campaigns))
    for month in MONTHS:
        first,last=bounds(month)
        for camp in campaigns:
            cid=str(camp['id']);key=f'yandex/{cid}/{month}'
            if completed(key):continue
            token=None;seen=set();n=0
            try:
                for page in range(1000):
                    space_check()
                    response=requests.post(ym.API+f'/campaigns/{cid}/stats/orders',headers=h,
                           params={'page_token':token}if token else{},json={'dateFrom':first,'dateTo':last},timeout=(10,60))
                    if response.status_code==403:
                        mark(key,status='unavailable',http=403);break
                    response.raise_for_status();value=response.json().get('result')
                    if not isinstance(value,dict)or not isinstance(value.get('orders'),list):
                        raise RuntimeError('invalid_yandex_orders_response')
                    rows=[{'account':'ya_acc1','campaign_id':cid,'order_id':str(o['id']),'payload':Json(o)}
                          for o in value['orders']if o.get('id')is not None]
                    if len(rows)!=len(value['orders']):raise RuntimeError('missing_order_identity')
                    n+=db.upsert('raw_yandex_stats_order',rows,conflict_cols=['account','order_id'])if rows else 0
                    nxt=(value.get('paging')or{}).get('nextPageToken')
                    if not nxt:
                        mark(key,status='done',rows=n,first=first,last=last);break
                    if nxt in seen:raise RuntimeError('repeated_yandex_cursor')
                    seen.add(nxt);token=nxt;time.sleep(.8)
                else:raise RuntimeError('yandex_page_budget_exceeded')
            except Exception as e:
                code=getattr(getattr(e,'response',None),'status_code',None)
                mark(key,status='failed',error=type(e).__name__,http=code)
                if code==401:return
            time.sleep(.8)


def adapt_ozon():
    from collectors import ozon_accrual_adapter as adapter
    for account in ['oz_acc1','oz_acc2']:
        for month in MONTHS:
            key=f'ozon_adapter/{account}/{month}'
            if completed(key):continue
            try:
                first,last=bounds(month)
                ops=adapter._load(account,first,last)
                n=adapter.save(account,ops,first,last)if ops else 0
                mark(key,status='done',rows=len(ops),saved=n)
            except Exception as e:mark(key,status='failed',error=type(e).__name__)


def narrow_ms_read(original_get, path, cutoff, progress=lambda **kw:None, tries=0):
    """Preserve a native page while reading <=20 rows and excluding today's writes."""
    parts=urlsplit(path);query=parse_qs(parts.query,keep_blank_values=True)
    if parts.path in ('/entity/demand','/entity/salesreturn'):
        query['filter']=[query.get('filter',[''])[0]+';moment<'+cutoff+' 00:00:00']
    limit=int(query.get('limit',['0'])[0])
    if limit<=20:
        return original_get(urlunsplit(parts._replace(query=urlencode(query,doseq=True))),tries)
    start=int(query.get('offset',['0'])[0]);merged=[];first=None
    for offset in range(start,start+limit,20):
        query['limit']=[str(min(20,start+limit-offset))];query['offset']=[str(offset)]
        page=original_get(urlunsplit(parts._replace(query=urlencode(query,doseq=True))),tries)
        if first is None:first=page
        elif page.get('meta',{}).get('size')!=first.get('meta',{}).get('size'):
            raise RuntimeError('MS_collection_changed_during_read')
        rows=page.get('rows',[]);merged.extend(rows)
        progress(collection=parts.path,offset=offset,rows=len(rows),total=page.get('meta',{}).get('size'))
        if len(rows)<int(query['limit'][0]) or offset+len(rows)>=page.get('meta',{}).get('size',0):break
    return {**first,'rows':merged}


def retry_ms_read(read, path, tries=0, *, attempts=5, sleep=time.sleep):
    """Retry only transient read failures; HTTP authorization failures stay failures."""
    for attempt in range(attempts):
        try:
            return read(path,tries)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError,TimeoutError,ConnectionError,
                http.client.RemoteDisconnected,ssl.SSLError):
            if attempt==attempts-1:raise
            sleep(min(60,2**(attempt+1)))


def cost_sources(only_accounts=None,min_interval=.5):
    from collectors import ms_demand_cogs as demands
    demands.MIN_INTERVAL=min_interval
    original_get=demands.get
    counters={'api_pages':0,'rows_read':0}
    def progress(**kw):
        counters['api_pages']+=1;counters['rows_read']+=kw['rows']
        if counters['api_pages']%20==0:
            with STATE_LOCK:
                STATE['cost_progress']={**counters,**kw};save()
            emit('cost_read_progress',**counters,**kw)
    def narrow_get(path,_tries=0):
        def reliable_get(page,tries):return retry_ms_read(original_get,page,tries)
        return narrow_ms_read(reliable_get,path,AS_OF.isoformat(),progress,_tries)
    demands.get=narrow_get
    from collectors import ms_return_cogs as returns
    for platform,accounts in [('wb',['wb_acc1','wb_acc2']),('ozon',['oz_acc1','oz_acc2'])]:
        for account in accounts:
            if only_accounts is not None and account not in only_accounts:continue
            key=f'ms_demand/{account}'
            try:
                space_check()
                count,missing=demands.collect(account,platform,moment_from='2025-09-01',batch=20,progress_every=100)
                mark(key,status='done',collected=count,missing_document_names=missing)
                key=f'ms_return/{account}'
                count,total=returns.collect(account,platform,moment_from='2026-01-01')
                mark(key,status='done',rows=count)
            except Exception as e:mark(key,status='failed',error=type(e).__name__)


def summarize():
    tables=['raw_wb_report','raw_wb_finance_detail','raw_ozon_accrual','raw_ozon_realization',
            'raw_ozon_transaction','raw_yandex_stats_order','ms_demand_cogs','ms_demand_pos','ms_return_cogs']
    counts={t:db.query('SELECT count(*) n FROM public.'+t)[0]['n']for t in tables}
    with STATE_LOCK:
        statuses={}
        for value in STATE.get('tasks',{}).values():
            status=value.get('status','unknown');statuses[status]=statuses.get(status,0)+1
        STATE['summary']={'rows':counts,'tasks':statuses,'production_published':False,
                          'financial_marts_built':False,'next_step':'review source completeness and COGS coverage before publishing'}
        save()
    emit('summary',**STATE['summary'])


def run():
    global STATE
    if not(ROOT/'initialized.json').exists():raise RuntimeError('stage_not_initialized')
    statefile=ROOT/'checkpoint.json'
    STATE=json.loads(statefile.read_text())if statefile.exists()else{'database':STAGE,'tasks':{}}
    db.DATABASE_URL=STAGE_DSN;os.environ['DATABASE_URL']=STAGE_DSN
    with db.get_conn()as c:
        with c.cursor()as q:
            q.execute('SELECT current_database()');assert q.fetchone()[0]==STAGE
    STATE['phase']='collecting_financial_sources';save()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5)as pool:
        lanes=[(wb_lane,'wb_acc1'),(wb_lane,'wb_acc2'),(ozon_lane,'oz_acc1'),(ozon_lane,'oz_acc2'),(yandex_lane,None)]
        pending={pool.submit(fn,*([arg]if arg else[])):fn.__name__+':'+str(arg)for fn,arg in lanes}
        for future in concurrent.futures.as_completed(pending):
            try:future.result()
            except Exception as e:mark('lane/'+pending[future],status='failed',error=type(e).__name__)
    STATE['phase']='adapting_ozon_sources';save();adapt_ozon()
    STATE['phase']='recovering_document_costs';save();summarize();cost_sources()
    STATE['phase']='ready_for_source_review';save();summarize()


if __name__=='__main__':
    os.umask(0o077)
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(ROOT/'worker.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if sys.argv[1:] == ['init']:initialize()
    elif sys.argv[1:] == ['run']:run()
    else:raise SystemExit('Use init or run')
