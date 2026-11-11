# поток: mkt / ozon
"""Owner-authorized wave1 activation after fresh minima and installed monthly stop."""
import argparse
import calendar
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path('/opt/mp-analytics'); sys.path.insert(0, str(ROOT))
from collectors import ozon_ads as ads, ozon as seller, ozon_bids
from ops.ozon_acc2_experiment_guard import validate_bindings
import requests

P = Path(__file__).parent; LAUNCH = P.parent; TZ = ZoneInfo('Europe/Moscow')
parser = argparse.ArgumentParser(); parser.add_argument('--execute', action='store_true'); args = parser.parse_args()
CFG_PATH = LAUNCH / 'risk_policy.json'; config = json.loads(CFG_PATH.read_text())
binding = validate_bindings(config)
authorization = json.loads((P/'launch_authorization.json').read_text())
assert authorization['account'] == 'oz_acc2' and authorization['launch_authorized']
assert authorization['composition_change_authorized'] and not authorization['composition_question_pending'], 'Await explicit answer about the 19 prelaunch exclusions'
targets = [c for c in binding['campaigns'] if c['wave'] == 1]
assert {c['campaign_id'] for c in targets} == {'50080606','50080615','50080620','50080623'}
assert config['owner_authorized_wave1_activation'] and not config['deletion_allowed']
assert config['risk_policy']['automatic_stop_rub'] == 1000
assert config['sku_risk_action'] == 'PAUSE_CAMPAIGN_KEEP_COMPOSITION'
assert config['owner_weekly_reference_rub'] == 12500
coverage = json.loads((P / 'baseline/coverage.json').read_text())
assert dt.date.fromisoformat(coverage['through_msk']) == dt.datetime.now(TZ).date() - dt.timedelta(days=1), 'Refresh the dated baseline before launch'
if not args.execute:
    print(json.dumps({'state':'READY_DRY_RUN','campaign_ids':[c['campaign_id'] for c in targets],
          'sku_count':sum(c['uploaded_skus'] for c in targets),'reference_weekly_rub':12500,
          'automatic_stop_zero_orders_rub':1000,'deletion_allowed':False,'network_calls':0})); sys.exit(0)

RUN = P / ('run_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S')); RUN.mkdir()
header = {'Authorization': 'Bearer ' + ads._token('oz_acc2')}; seller_header = seller._headers('oz_acc2')
ids = {c['campaign_id'] for c in targets}


def now():
    return dt.datetime.now(dt.timezone.utc)


def save(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp'); temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n'); temp.replace(path)


def event(obj):
    with (LAUNCH / 'activation_events.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc':now().isoformat(),**obj},ensure_ascii=False)+'\n'); f.flush(); os.fsync(f.fileno())


def call(method, path, body=None, seller_api=False):
    is_write = not seller_api and method in ('PUT','POST') and path != '/api/client/min/sku'
    if is_write:
        cid = path.split('/')[4]
        assert cid in ids and path in [f'/api/client/campaign/{cid}/activate',f'/api/client/campaign/{cid}/deactivate',f'/api/client/campaign/{cid}/products']
        event({'phase':'WRITE_INTENT','wave':1,'method':method,'path':path,'body':body})
    url = path if seller_api else ads.PERF + path
    for attempt in range(8):
        # Ambiguous network failure is never blindly retried for activation.
        r = requests.request(method,url,headers=seller_header if seller_api else header,json=body,timeout=90)
        obj = r.json() if r.content else {}
        stamp = now().strftime('%H%M%S_%f')
        save(RUN/f'{stamp}.json',{'at_utc':now().isoformat(),'method':method,'path':path,
             'request':body,'http':r.status_code,'response':obj})
        if r.status_code != 429:
            break
        time.sleep(min(20, max(3, float(r.headers.get('Retry-After') or 5))))
    if is_write:
        event({'phase':'WRITE_RESPONSE','wave':1,'method':method,'path':path,'http':r.status_code})
    r.raise_for_status(); return obj


def current():
    return {str(c['id']):c for c in call('GET','/api/client/campaign')['list']}


def next_month(value):
    local = value.astimezone(TZ).replace(microsecond=0)
    year = local.year + (local.month == 12); month = local.month % 12 + 1
    return local.replace(year=year,month=month,day=min(local.day,calendar.monthrange(year,month)[1]))


def arm_deadline(start, phase):
    stop = next_month(start)
    manifest = {'account':'oz_acc2','scheduled_stop_at_msk':stop.isoformat(),
          'scheduled_stop_at_utc':stop.astimezone(dt.timezone.utc).isoformat(),
          'month_policy':'one common calendar month from first successful wave1 activation',
          'start_basis':phase,'first_start_at_utc':start.isoformat(),
          'forbidden_existing_campaign_ids':binding['forbidden_existing_campaign_ids'],
          'campaigns':[{'key':c['plan_key'],'title':c['title']} for c in binding['campaigns']]}
    mp = RUN/f'calendar_stop_{phase}.json'; save(mp,manifest); digest=hashlib.sha256(mp.read_bytes()).hexdigest()
    bp = RUN/f'calendar_stop_{phase}_bindings.json'
    save(bp,{'account':'oz_acc2','manifest_sha256':digest,'campaigns':[
         {'plan_key':c['plan_key'],'campaign_id':c['campaign_id']} for c in binding['campaigns']]})
    operator = ROOT/'ops/ozon_acc2_experiment_deadline_stop.py'
    command = [str(ROOT/'venv/bin/python'),str(operator),'--manifest',str(mp),'--manifest-sha256',digest,'--bindings',str(bp)]
    proof = subprocess.run(command,capture_output=True,text=True,check=True)
    save(RUN/f'calendar_stop_{phase}_dry_run.json',json.loads(proof.stdout))
    service = ROOT/'ops/ozon-acc2-experiment-month-end.service'
    service.write_text('# поток: mkt / ozon\n[Unit]\nDescription=Stop bound Ozon monthly experiment\nAfter=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType=oneshot\nWorkingDirectory=/opt/mp-analytics\nExecStart='+' '.join(command+['--execute'])+'\nRestart=on-failure\nRestartSec=30\nTimeoutStartSec=10min\n')
    timer = ROOT/'ops/ozon-acc2-experiment-month-end.timer'
    timer.write_text('# поток: mkt / ozon\n[Unit]\nDescription=Ozon experiment calendar month end\n\n[Timer]\nOnCalendar='+stop.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')+'\nPersistent=true\nAccuracySec=1s\nUnit=ozon-acc2-experiment-month-end.service\n\n[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemd-analyze','verify',str(service),str(timer)],check=True,capture_output=True,text=True)
    for file in [service,timer]:
        shutil.copy2(file,Path('/etc/systemd/system')/file.name)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','ozon-acc2-experiment-month-end.timer'],check=True,capture_output=True)
    assert subprocess.check_output(['systemctl','is-active','ozon-acc2-experiment-month-end.timer'],text=True).strip()=='active'
    cfg = json.loads(CFG_PATH.read_text()); cfg.update(monthly_deadline_bound=True,
         calendar_stop_at_msk=stop.isoformat(),calendar_stop_at_utc=stop.astimezone(dt.timezone.utc).isoformat(),
         calendar_stop_manifest=str(mp),calendar_stop_manifest_sha256=digest,calendar_stop_bindings=str(bp),
         calendar_start_basis=phase)
    save(CFG_PATH,cfg)
    return stop


cs = current()
assert all(cs[c['campaign_id']]['title']==c['title'] and cs[c['campaign_id']]['state']=='CAMPAIGN_STATE_INACTIVE' for c in targets), 'Reconcile existing activation before retry'
assert all(cs[c['campaign_id']]['state']=='CAMPAIGN_STATE_INACTIVE' for c in binding['campaigns'] if c['wave']!=1), 'Later waves already active; reconcile before starting'
with (P/'READY_3851_NOT_APPLIED.csv').open(encoding='utf-8-sig') as f:
    rows = [r for r in csv.DictReader(f) if r['actual_campaign_id'] in ids]
assert len(rows)==3851
for c in targets:
    products, code = ozon_bids._campaign_products(header,c['campaign_id']); assert code==200
    assert {str(p['sku']) for p in products}=={r['sku'] for r in rows if r['actual_campaign_id']==c['campaign_id']}

# Reuse the narrow fresh preflight; never repeat the whole catalog or 28-day baseline.
preflight = P/'preflight'
summary = json.loads((preflight/'preflight_summary.json').read_text())
assert summary['account']=='oz_acc2' and summary['eligible_for_upload']==3851
assert (now()-dt.datetime.fromisoformat(summary['finished_at_utc'])).total_seconds() < 3600, 'Refresh expired preflight'
cards={}
for file in preflight.glob('cards_batch_*.json'):
    z=json.loads(file.read_text());assert z['account']=='oz_acc2'
    cards.update({str(x['id']):x for x in z['response']['items']})
readiness=[]
for r in rows:
    x=cards[r['product_id']]; assert x['offer_id']==r['offer_id']
    stocks=x.get('stocks') or {}; available=sum(max(D(0),D(str(s.get('present') or 0))-D(str(s.get('reserved') or 0))) for s in stocks.get('stocks') or [])
    readiness.append({'sku':r['sku'],'own_has_stock':stocks.get('has_stock') is True,'available_stock':str(available),
       'available_channel':any(a.get('availability')=='AVAILABLE' for a in x.get('availabilities') or []),
       'own_errors_count':len(x.get('errors') or []),'archived':bool(x.get('is_archived') or x.get('is_autoarchived'))})
save(RUN/'own_readiness.json',readiness)
assert not any(x['archived'] or x['own_errors_count'] or not x['own_has_stock'] or D(x['available_stock'])<=0 or not x['available_channel'] for x in readiness), 'Fresh structural own-card defect requires review without deleting SKU'
skus=[r['sku'] for r in rows]
fresh_floors=json.loads((preflight/'individual_floors.json').read_text())
floor={sku:fresh_floors[sku]['bid_micro_rub'] for sku in skus}
assert set(floor)==set(skus);save(RUN/'individual_minima_micro_rub.json',floor)
changed=0
for c in targets:
    products, code=ozon_bids._campaign_products(header,c['campaign_id']); assert code==200
    todo=[{'sku':str(x['sku']),'bid':floor[str(x['sku'])]} for x in products if D(str(x['bid']))!=D(floor[str(x['sku'])])]
    for i in range(0,len(todo),100):call('PUT',f"/api/client/campaign/{c['campaign_id']}/products",{'bids':todo[i:i+100]})
    changed+=len(todo)
    products,code=ozon_bids._campaign_products(header,c['campaign_id']); assert code==200
    assert all(D(str(x['bid']))==D(floor[str(x['sku'])]) for x in products)
    save(RUN/f"prestart_readback_{c['campaign_id']}.json",products)
print('Fresh minimum readback verified, changed bids',changed,flush=True)
stop=arm_deadline(now(),'preactivation_safety')
subprocess.run(['systemctl','start','ozon-acc2-experiment-guard.timer'],check=True)
subprocess.run(['systemctl','start','ozon-acc2-experiment-guard.service'],check=True)
assert subprocess.check_output(['systemctl','show','ozon-acc2-experiment-guard.service','-p','Result','--value'],text=True).strip()=='success'
save(RUN/'guard_immediately_before_activation.json',json.loads((LAUNCH/'guard_status.json').read_text()))
# Freeze today's general sales immediately before the first activation, separately from the 28 full days.
from collectors.ozon_postings import fetch_postings
from core.ozon_posting import product_price
precut=now(); day_start=precut.astimezone(TZ).replace(hour=0,minute=0,second=0,microsecond=0)
maps=json.loads(Path(config['family_maps_path']).read_text())
target_skus=set(skus)
target_models={maps['model_by_sku_alias'].get(s) for s in skus}-{None,''}
target_families={maps['article_family_by_sku_alias'].get(s) for s in skus}-{None,''}
pre_scopes={k:{'orders':set(),'ordered_value_rub':D(0)} for k in ['account','own_SKUs','native_models','article_families']}
for scheme in ['fbs','fbo']:
    posts=fetch_postings('oz_acc2',scheme,day_start.isoformat(),precut.astimezone(TZ).isoformat(),limit=100,window_budget=240)
    save(RUN/f'postings_before_start_{scheme}.json',{'account':'oz_acc2','scheme':scheme,'from_msk':day_start.isoformat(),'through_msk':precut.astimezone(TZ).isoformat(),'captured_at_utc':now().isoformat(),'complete':True,'postings':posts})
    for post in posts:
        if post['status'].lower().startswith('cancel'):continue
        oid=str(post['order_id'])
        for prod in post['products']:
            raw=str(prod['sku']); canonical=maps['canonical_by_alias'].get(raw,raw)
            val=product_price(prod)*D(str(prod['quantity']))
            for key,yes in [('account',True),('own_SKUs',canonical in target_skus),('native_models',maps['model_by_sku_alias'].get(raw) in target_models),('article_families',maps['article_family_by_sku_alias'].get(raw) in target_families)]:
                if yes:pre_scopes[key]['orders'].add(oid);pre_scopes[key]['ordered_value_rub']+=val
save(RUN/'account_immediately_before_activation.json',{'account':'oz_acc2','from_msk':day_start.isoformat(),'through_msk':precut.astimezone(TZ).isoformat(),'scopes':{k:{'unique_orders':len(v['orders']),'ordered_value_rub':str(v['ordered_value_rub'])} for k,v in pre_scopes.items()},'ordered_value_is_financial_revenue':False,'captured_before_activation':True})
started=[]; first=None
try:
    for c in targets:
        cid=c['campaign_id']
        event({'phase':'ACTIVATE_INTENT','wave':1,'campaign_id':cid,'owner_reference_weekly_rub':12500})
        try:reply=call('POST',f'/api/client/campaign/{cid}/activate')
        except requests.RequestException:
            if current()[cid]['state']!='CAMPAIGN_STATE_RUNNING':raise
        state=None
        for attempt in range(12):
            state=current()[cid]['state']
            if state=='CAMPAIGN_STATE_RUNNING':break
            time.sleep(3)
        assert state=='CAMPAIGN_STATE_RUNNING', f'{cid} activation not RUNNING: {state}'
        at=now(); started.append(cid)
        event({'phase':'RUNNING_CONFIRMED','wave':1,'campaign_id':cid,'at_utc':at.isoformat()})
        if first is None:first=at;stop=arm_deadline(first,'first_running_confirmed')
        print('RUNNING CONFIRMED',cid,c['title'],flush=True)
    final=current();assert all(final[c['campaign_id']]['state']=='CAMPAIGN_STATE_RUNNING' for c in targets)
    assert all(final[c['campaign_id']]['state']=='CAMPAIGN_STATE_INACTIVE' for c in binding['campaigns'] if c['wave']!=1)
    status={'state':'WAVE1_RUNNING_CONFIRMED','first_running_confirmed_at_utc':first.isoformat(),
         'all_wave1_running_confirmed_at_utc':now().isoformat(),'stop_at_msk':stop.isoformat(),
         'wave1_campaign_ids':started,'wave1_uploaded_skus':3851,'floor_refresh_run':str(RUN),
         'minima_refreshed':len(floor),'changed_bids':changed,'other_waves_not_created':True,
         'proposed_wave2_at_msk':(first.astimezone(TZ)+dt.timedelta(days=7)).isoformat(),
         'later_wave_activation_scheduled':False,'account':'oz_acc2','deferred_prelaunch_SKUs':19,'campaigns':[final[c['campaign_id']] for c in binding['campaigns']]}
    save(LAUNCH/'activation_status.json',status);event({'phase':'ALL_WAVE1_RUNNING_CONFIRMED','wave':1,'campaign_ids':started})
except Exception as exc:
    observed=current()
    for c in targets:
        cid=c['campaign_id']
        if observed[cid]['state']=='CAMPAIGN_STATE_RUNNING':call('POST',f'/api/client/campaign/{cid}/deactivate')
    save(LAUNCH/'activation_status.json',{'state':'ACTIVATION_FAILED_ROLLED_BACK','error_type':type(exc).__name__,
          'error':str(exc)[:500],'had_confirmed_running':started,'at_utc':now().isoformat(),'run_dir':str(RUN)})
    raise
subprocess.run(['systemctl','start','ozon-acc2-experiment-guard.service'],check=True)
print('WAVE1 LIVE; remaining waves inactive; monthly stop',stop.isoformat(),flush=True)
