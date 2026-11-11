# поток: mkt / WB
"""One bounded START retry for three independently confirmed paused CPC campaigns.

No budget write exists here. Run only after the first accepted start stayed unapplied.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.wb_ads import B, _token
from ops.wb_ad_campaign_new import start

P = Path(__file__).parent
IDS = [35697852, 35698099, 35699026]
ap = argparse.ArgumentParser(); ap.add_argument('--execute', action='store_true'); args = ap.parse_args()
if not args.execute:
    print('DRY_RUN: only retry START for3 confirmed paused/funded CPC IDs; no deposit calls')
    raise SystemExit(0)
assert not (P / 'CANCELLED_BY_USER.json').exists()
journalfile = P / 'start_retry_once.jsonl'
assert not journalfile.exists(), 'Retry already attempted; reconcile, never repeat blindly'
auth = {'Authorization': _token('wb_acc1')}


def record(obj):
    with journalfile.open('a') as f:
        f.write(json.dumps({'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), **obj}, ensure_ascii=False)+'\n')
        f.flush(); os.fsync(f.fileno())


r = requests.get(B+'/api/advert/v3/adverts', headers=auth, params={'advertIds':','.join(map(str,IDS))}, timeout=45)
r.raise_for_status(); rows = r.json()['adverts']
assert {c['advertId'] for c in rows} == set(IDS)
assert all(c['status'] == 11 and c['settings']['paymentType']=='cpc' for c in rows)
r = requests.post(B+'/api/advert/v2/budget', headers=auth, json={'advertIds':IDS}, timeout=45)
r.raise_for_status(); budgets = r.json()['adverts']
assert {b['advertId'] for b in budgets} == set(IDS) and all(b['total'] >= 1000 for b in budgets)
record({'phase':'INDEPENDENT_PRECHECK','campaigns':rows,'budgets':budgets})
original = requests.sessions.Session.request
for cid in IDS:
    assert not (P / 'CANCELLED_BY_USER.json').exists()
    record({'phase':'INTENT','campaign_id':cid,'kind':'START_RETRY_ONLY_NO_DEPOSIT'})

    def capture(session, method, url, **kw):
        assert method.upper()=='GET' and url==B+'/adv/v0/start' and kw['params']=={'id':cid}
        kw['headers'] = {**kw['headers'], 'Cache-Control':'no-cache', 'Pragma':'no-cache'}
        response = original(session,method,url,**kw)
        record({'phase':'RESPONSE','campaign_id':cid,'http':response.status_code,'response':response.text[:1000],
                'cache_headers':{k:v for k,v in response.headers.items() if k.lower() in ['date','age','cache-control','x-cache','cf-cache-status']}})
        return response

    requests.sessions.Session.request = capture
    try:code,_=start('wb_acc1',cid)
    finally:requests.sessions.Session.request=original
    assert code==200
    print('START RETRY ONLY',cid,'HTTP200',flush=True)
    time.sleep(2)
print('Start retry complete; no further retries; independent readback required',flush=True)
