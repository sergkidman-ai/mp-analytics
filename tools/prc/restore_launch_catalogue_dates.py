# поток: prc
"""Restore all tracked model dates from CREATE audits, including archived MS products.
Read-only upstream. Local evidence changes require --apply. Cache permits resuming;
--retry-transient retries API failures without re-reading absent CREATE histories.
Example: PYTHONPATH=. venv/bin/python tools/prc/restore_launch_catalogue_dates.py --output .private/launch_dates
"""
import argparse

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections import defaultdict,Counter
from datetime import datetime,timezone,timedelta
from uuid import UUID
import json,gzip,time,sys
from core import db,ms_api


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--output',required=True)
 parser.add_argument('--workers',type=int,default=2)
 parser.add_argument('--retry-transient',action='store_true')
 parser.add_argument('--apply',action='store_true')
 args=parser.parse_args()
 ROOT=Path(args.output).resolve();ROOT.mkdir(parents=True,exist_ok=True)
 C=ROOT/'catalog';C.mkdir(exist_ok=True);A=ROOT/'audit_by_id';A.mkdir(exist_ok=True)
 ms_api.TIMEOUT=25;ms_api.RETRIES=3
 ZONE=timezone(timedelta(hours=3));EPOCH=datetime(1582,10,15,tzinfo=timezone.utc)
 retry=args.retry_transient

 def dump(path,value):
  temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,default=str));temp.replace(path)

 def idtime(card):
  u=UUID(card['ms_id'])
  if u.fields[2]>>12!=1:raise ValueError('not_time_id')
  value=EPOCH+timedelta(microseconds=u.time//10)
  if value>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('future_time_id')
  return value

 def catalogue_page(offset):
  path=C/f'{offset:06}.json.gz'
  if path.exists():
   try:
    with gzip.open(path,'rt') as f:return json.load(f)
   except (OSError,EOFError,ValueError):pass
  page=ms_api.get('/entity/product',{'limit':1000,'offset':offset,'filter':'archived=true;archived=false'})
  temp=path.with_suffix('.gz.tmp')
  with gzip.open(temp,'wb',compresslevel=1) as f:f.write(json.dumps(page,ensure_ascii=False).encode())
  temp.replace(path)
  return page

 first=catalogue_page(0);total=first.get('meta',{}).get('size')
 if not isinstance(total,int):raise ValueError('catalogue_size_unknown')
 collected={0:first}
 with ThreadPoolExecutor(max_workers=min(2,max(1,args.workers))) as pool:
  futures={pool.submit(catalogue_page,n):n for n in range(1000,total,1000)}
  for n,f in enumerate(as_completed(futures),1):
   collected[futures[f]]=f.result()
   if n%10==0:print('catalogue pages',n+1,'/',(total+999)//1000,flush=True)
 cards=[];seen=set()
 for offset,page in sorted(collected.items()):
  if page.get('meta',{}).get('size')!=total:raise ValueError('catalogue_generation_changed')
  for card in page.get('rows',[]):
   if card['id'] in seen:raise ValueError('duplicate_catalogue_identity')
   seen.add(card['id']);cards.append(dict(ms_id=card['id'],code=card.get('code'),external_code=card.get('externalCode'),archived=card.get('archived'),name=card.get('name')))
 if len(cards)!=total:raise ValueError('catalogue_incomplete')
 collected.clear()
 dump(ROOT/'catalogue.json',cards)
 scope={r['external_code'] for r in db.query('SELECT external_code FROM prc_launch')}
 groups=defaultdict(list)
 for card in cards:
  if card['external_code'] in scope:groups[card['external_code']].append(card)
 print('catalogue complete',len(cards),'archived',sum(bool(c['archived']) for c in cards),'tracked models present',len(groups),'missing',len(scope-set(groups)),flush=True)

 def read_create(card):
  pid=card['ms_id'];path=A/(pid+'.json')
  if path.exists():
   cached=json.loads(path.read_text())
   if 'error' not in cached or not retry or cached['error'].startswith('ValueError:'):return cached
  try:
   url='/entity/product/'+pid+'/audit';pages=[ms_api.get(url,{'limit':20})]
   size=pages[0].get('meta',{}).get('size')
   if not isinstance(size,int) or not size:raise ValueError('audit_missing')
   if size>len(pages[0].get('rows',[])):pages.append(ms_api.get(url,{'limit':20,'offset':max(0,size-20)}))
   unique={}
   for page in pages:
    for event in page.get('rows',[]):
     eid=event.get('entity',{}).get('meta',{}).get('href','').split('/')[-1]
     if event.get('eventType')=='create' and eid==pid:unique[(event['moment'],eid)]=event
   if len(unique)!=1:raise ValueError('unambiguous_create_not_found')
   event=next(iter(unique.values()));moment=datetime.fromisoformat(event['moment']).replace(tzinfo=ZONE)
   if moment>datetime.now(ZONE)+timedelta(minutes=5):raise ValueError('future_audit_creation')
   result=dict(ms_id=pid,ms_created=moment.date().isoformat(),moment=event['moment'],event=event,audit_pages=pages)
   try:result['id_time_matches']=abs((moment-idtime(card)).total_seconds())<=300
   except ValueError:result['id_time_matches']=None
  except Exception as e:result=dict(ms_id=pid,error=type(e).__name__+':'+str(e)[:180],audit_pages=locals().get('pages',[]))
  dump(path,result);return result

 def recover(item):
  ext,items=item;timed=[];unknown=[]
  for card in items:
   try:timed.append((idtime(card),card))
   except ValueError:unknown.append(card)
  candidates=([min(timed,key=lambda v:v[0])[1]] if timed else [])+unknown
  found=[]
  for card in candidates:
   found.append(read_create(card))
   if 'error' in found[-1]:return dict(external_code=ext,errors=[found[-1]],cards=len(items))
  # A time-based ID that disagrees with CREATE is an imported identity; audit all cards.
  if found and found[0].get('id_time_matches') is False:
   done={r['ms_id'] for r in found};found.extend(read_create(card) for card in items if card['ms_id'] not in done)
  errors=[r for r in found if 'error' in r]
  if errors:return dict(external_code=ext,errors=errors,cards=len(items))
  earliest=min(found,key=lambda r:r['ms_created'])
  return dict(external_code=ext,ms_created=earliest['ms_created'],ms_id=earliest['ms_id'],
              source_ref=str((A/(earliest['ms_id']+'.json')).resolve()),catalogue_ref=str((ROOT/'catalogue.json').resolve()),cards=len(items),audit_candidates=len(found))

 records=[];unresolved=[];started=time.monotonic()
 with ThreadPoolExecutor(max_workers=min(4,max(1,args.workers))) as pool:
  futures={pool.submit(recover,item):item[0] for item in sorted(groups.items())}
  for n,future in enumerate(as_completed(futures),1):
   r=future.result();(unresolved if 'errors' in r else records).append(r)
   if n%100==0 or n==len(futures):
    status=dict(processed=n,total=len(futures),verified=len(records),unresolved=len(unresolved),seconds=round(time.monotonic()-started))
    dump(ROOT/'progress.json',status);dump(ROOT/'records.json',records);dump(ROOT/'unresolved.json',unresolved)
    print(json.dumps(status),flush=True)
 dump(ROOT/'complete.json',dict(verified=len(records),unresolved=len(unresolved),catalogue_count=len(cards),missing_models=sorted(scope-set(groups))))
 print('complete',json.dumps(json.loads((ROOT/'complete.json').read_text())),flush=True)

 if args.apply:
  backup=ROOT/'local_dates_before_apply.json.gz'
  if not backup.exists():
   snapshot={t:db.query('SELECT * FROM '+t) for t in ('prc_launch','prc_launch_creation_evidence')}
   with gzip.open(backup,'wb',compresslevel=1) as f:
    f.write(json.dumps(snapshot,ensure_ascii=False,default=str).encode())
  from tools.prc.restore_launch_dates import apply_evidence
  applied=apply_evidence(records,allow_earlier=True)
  dump(ROOT/'applied.json',applied)
  print('applied',json.dumps(applied),flush=True)

if __name__=="__main__":
 main()
