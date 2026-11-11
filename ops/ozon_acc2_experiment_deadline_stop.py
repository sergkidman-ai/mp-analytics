# поток: mkt
"""Prepared stop-only operator. Default is offline preview; not installed or armed."""
import argparse,datetime as dt,hashlib,json,pathlib,sys,time
sys.dont_write_bytecode=True
ROOT=pathlib.Path('/opt/mp-analytics');sys.path.insert(0,str(ROOT))
def bindings(manifest,b):
 assert manifest['account']=='oz_acc2' and b['account']=='oz_acc2','Account mismatch'
 forbidden=set(manifest['forbidden_existing_campaign_ids']);plans={c['key']:c for c in manifest['campaigns']}
 entries=b.get('campaigns',[])
 assert len(entries)==len(plans) and {x['plan_key'] for x in entries}==set(plans),'New campaign bindings incomplete'
 ids=[str(x['campaign_id']) for x in entries]
 assert len(ids)==4 and set(ids)=={'50080606','50080615','50080620','50080623'},'Acc2 W1 exact scope mismatch'
 assert len(ids)==len(set(ids)) and all(i.isdigit() and i not in forbidden for i in ids),'Forbidden or duplicate campaign ID'
 return [(str(x['campaign_id']),plans[x['plan_key']]['title']) for x in entries]
def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--manifest-sha256',required=True);p.add_argument('--bindings',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
 path=pathlib.Path(a.manifest);assert hashlib.sha256(path.read_bytes()).hexdigest()==a.manifest_sha256,'Manifest hash mismatch'
 m=json.loads(path.read_text());b=json.loads(pathlib.Path(a.bindings).read_text())
 assert b.get('manifest_sha256')==a.manifest_sha256,'Binding manifest hash mismatch'
 try:targets=bindings(m,b)
 except AssertionError as e:
  print(json.dumps({'state':'BLOCKED_UNBOUND','reason':str(e),'execute_requested':a.execute}));return 2 if a.execute else 0
 due=dt.datetime.fromisoformat(m['scheduled_stop_at_msk']);now=dt.datetime.now(dt.timezone.utc)
 if not a.execute:
  print(json.dumps({'state':'DRY_RUN','targets':[x[0] for x in targets],'scheduled_stop':due.isoformat(),'due':now>=due,'network_calls':0}));return 0
 if now<due:
  print(json.dumps({'state':'NOT_DUE','scheduled_stop':due.isoformat(),'marketplace_changes':0}));return 0
 from collectors.ozon_ads import _token,PERF
 import requests
 h={'Authorization':'Bearer '+_token('oz_acc2')}
 # Refuse accidental targeting of any unrelated account campaign by checking exact bound titles.
 r=requests.get(PERF+'/api/client/campaign',headers=h,params={'campaignIds':[x[0] for x in targets]},timeout=60);r.raise_for_status();got={str(x['id']):x for x in r.json().get('list',[])}
 assert all(cid in got and got[cid]['title']==title for cid,title in targets),'Bound campaign title/identity mismatch'
 for attempt in range(3):
  for cid,_ in targets:
   if got[cid]['state'] in ['CAMPAIGN_STATE_INACTIVE','CAMPAIGN_STATE_FINISHED','CAMPAIGN_STATE_ARCHIVED']:continue
   rr=requests.post(PERF+f'/api/client/campaign/{cid}/deactivate',headers=h,timeout=60);rr.raise_for_status()
  r=requests.get(PERF+'/api/client/campaign',headers=h,params={'campaignIds':[x[0] for x in targets]},timeout=60);r.raise_for_status();got={str(x['id']):x for x in r.json().get('list',[])}
  if all(cid in got and got[cid]['state'] in ['CAMPAIGN_STATE_INACTIVE','CAMPAIGN_STATE_FINISHED','CAMPAIGN_STATE_ARCHIVED'] for cid,_ in targets):
   print(json.dumps({'state':'STOP_CONFIRMED','at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'campaign_ids':[x[0] for x in targets]}));return 0
  time.sleep(5)
 raise RuntimeError('Stop not confirmed; service must retry and operator must check')
if __name__=='__main__':
 try:sys.exit(main())
 except Exception as e:
  print(json.dumps({'state':'ERROR','error_type':type(e).__name__,'reason':str(e)[:160]}));sys.exit(1)
