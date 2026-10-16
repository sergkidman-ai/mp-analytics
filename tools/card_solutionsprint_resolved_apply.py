#!/usr/bin/env python3
# поток: card
"""Owner-authorized Solutions Print description changes, night October 13–14."""
import argparse
import csv
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from lxml import html

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import ms_api
from tools.card_rapid_description_sync import apply, checked_write_payload, stamp, window
from tools.card_solutionsprint_description_audit import fetch, normalized
from tools.card_solutionsprint_scraper import login, parse_card
from tools.card_solutionsprint_current_review import parse_stocks

SOURCE=ROOT/'docs/reports/card_solutionsprint_current_review_2026-10-12'
OUT=ROOT/'docs/reports/card_solutionsprint_resolved_sync_2026-10-13'
START=datetime(2026,10,13,20,20,tzinfo=timezone.utc)
CUTOFF=datetime(2026,10,14,2,tzinfo=timezone.utc)

def save(name,data):
    with (OUT/name).open('w') as f:
        f.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n');f.flush();os.fsync(f.fileno())

def prepare(use_preflight=False):
    if (OUT/'journal.jsonl').exists():raise RuntimeError('Existing journal; do not rebuild')
    review=json.loads((SOURCE/'review.json').read_text())
    cards={r['id']:r for r in json.loads((SOURCE/'scoped_ms_cards.json').read_text())}
    chip={r['id']:r for r in json.loads((SOURCE/'chip_information_review.json').read_text())}
    selected=[r for r in review if r['status']=='DESCRIPTION_DIFFERENCE' and r['stock']>=4]
    assert len(selected)==len({r['id'] for r in selected})==384
    plan=[]
    for r in selected:
        target=normalized(r['source_name']);evidence=chip.get(r['id'],{})
        suffix='';kind='SUPPLIER_NAME'
        if evidence.get('chip_source_supported'):
            if evidence['chip_badges']==['С чипом']:
                suffix=' (с чипом)';kind='NAME_PLUS_CHIP_BADGE'
            else:
                assert r['supplier_code'] in {'293069','293070'} and evidence['source_notes']
                suffix=' (новая версия чипа, для аппаратов, выпущенных после 2017 года)'
                kind='NAME_PLUS_CHIP_NOTE'
        elif evidence:
            assert evidence.get('user_decision')=='USE_EXACT_SUPPLIER_NAME'
            kind='OWNER_EXACT_SUPPLIER_NAME'
        plan.append({'id':r['id'],'external_code':r['external_code'],'article':r['article'],
                     'supplier_id':ms_api.meta_id(cards[r['id']],'supplier'),'old_description':r['old_description'],
                     'description':target+suffix,'source_name':target,'source_url':r['source_url'],
                     'supplier_code':r['supplier_code'],'warehouse':r['warehouse'],'stock_at_audit':r['stock'],
                     'basis':kind,'required_badges':evidence.get('chip_badges',[]) if suffix else [],
                     'required_notes':evidence.get('source_notes',[]) if suffix else []})
    assert sum(r['basis']=='NAME_PLUS_CHIP_BADGE' for r in plan)==4
    assert sum(r['basis']=='NAME_PLUS_CHIP_NOTE' for r in plan)==2
    assert sum(r['basis']=='OWNER_EXACT_SUPPLIER_NAME' for r in plan)==3
    def read(row):
        return ms_api.get('/entity/product/'+row['id'])
    if use_preflight:
        cached=OUT/'ms_preflight_all.json'
        assert time.time()-cached.stat().st_mtime<600
        by_id={r['id']:r for r in json.loads(cached.read_text())}
        assert set(by_id)=={r['id'] for r in plan}
        before=[by_id[r['id']] for r in plan]
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:before=list(pool.map(read,plan))
    save('ms_preflight_all.json',before)
    drift=[];refreshed=[];already=[];pending=[]
    for row,live in zip(plan,before):
        # The approved suffix is still needed if another importer already installed the base title.
        if row['basis'] in {'NAME_PLUS_CHIP_BADGE','NAME_PLUS_CHIP_NOTE'} and live.get('description')==row['source_name']:
            refreshed.append({'id':row['id'],'article':row['article'],'old_snapshot':row['old_description'],'live_description':live['description']})
            row['old_description']=live['description']
        try:
            patch=checked_write_payload(live,row)
            if patch:pending.append(row)
            else:already.append({'id':row['id'],'article':row['article'],'reason':'ALREADY_DESIRED'})
        except RuntimeError as exc:
            drift.append({'id':row['id'],'article':row['article'],'reason':str(exc),
                          'old_description':row['old_description'],'live_description':live.get('description'),
                          'target_description':row['description']})
    save('preflight_drift.json',drift)
    if drift:raise RuntimeError('Preflight drift saved; review before freezing')
    save('refreshed_base_titles.json',refreshed);save('already_desired.json',already)
    plan=pending
    plan.sort(key=lambda r:r['id']);save('plan.json',plan);save('ms_before.json',before)
    with (OUT/'plan.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(plan[0]),delimiter=';');w.writeheader();w.writerows(plan)
    save('summary.json',{'status':'scheduled_ready','at':stamp(),'changes':len(plan),'authorized_candidates':384,'already_desired':len(already),
                        'writes':0,'verified':0,'plan_sha256':hashlib.sha256((OUT/'plan.json').read_bytes()).hexdigest(),
                        'start_msk':'2026-10-13 23:20','cutoff_msk':'2026-10-14 05:00',
                        'write_fields':['description'],'max_batch':50,'pause_seconds':60,'canary':{'count':200,'pause_seconds':600}})
    print(f'Prepared {len(plan)} descriptions; already desired {len(already)}; refreshed base titles {len(refreshed)}',flush=True)

def run():
    summary=json.loads((OUT/'summary.json').read_text())
    if summary['status']!='scheduled_ready' or (OUT/'journal.jsonl').exists():raise RuntimeError('Not a fresh frozen plan')
    assert hashlib.sha256((OUT/'plan.json').read_bytes()).hexdigest()==summary['plan_sha256']
    plan=json.loads((OUT/'plan.json').read_text());assert len(plan)==summary['changes'] and len(plan)<=384
    def guard():
        waited=False
        while True:
            now=datetime.now(timezone.utc)
            if now<START or now>=CUTOFF or not window():raise RuntimeError('Outside authorized night')
            if subprocess.run(['systemctl','is-active','--quiet','prc-enter-buyprice.service']).returncode!=0:return waited
            waited=True;print(stamp(),'Waiting for buyprice writer',flush=True);time.sleep(30)
    guard()
    # Read all supplier pages before any writes; changed text/evidence aborts the run.
    local=threading.local();lock=threading.Lock();next_start=[0.0]
    def detail(item):
        code,url=item
        if not hasattr(local,'session'):local.session=requests.Session();login(local.session)
        with lock:
            time.sleep(max(0,next_start[0]-time.monotonic()));next_start[0]=time.monotonic()+.5
        guard()
        d=parse_card(html.tostring(fetch(local.session,url)),url,authenticated=True)
        assert d['supplier_code']==code
        return code,d
    urls={r['supplier_code']:r['source_url'] for r in plan}
    with ThreadPoolExecutor(max_workers=3) as pool:details=dict(pool.map(detail,urls.items()))
    save('supplier_before_write.json',details)
    eligible=[];skipped=[]
    for row in plan:
        d=details[row['supplier_code']]
        if normalized(d['name'])!=row['source_name']:raise RuntimeError('Supplier title changed: '+row['article'])
        if any(x not in d['chip_badges'] for x in row['required_badges']):raise RuntimeError('Chip badge changed')
        if any(normalized(x) not in {normalized(v) for v in d['primary_notes']} for x in row['required_notes']):raise RuntimeError('Chip note changed')
        stock=parse_stocks(d['availability_text']).get(row['warehouse'])
        if stock is None and d['stock_positive'] is not False:raise RuntimeError('Unknown relevant warehouse stock')
        if stock is None or stock<4:skipped.append({'id':row['id'],'article':row['article'],'stock':stock,'reason':'STOCK_BELOW_4'});continue
        eligible.append(row)
    save('skipped_before_write.json',skipped);save('execution_plan.json',eligible)
    summary.update(changes=len(eligible),skipped_stock=len(skipped),status='source_verified',at=stamp())
    save('summary.json',summary);guard()
    apply(OUT,eligible,summary,before_write=guard)

def main():
    parser=argparse.ArgumentParser();g=parser.add_mutually_exclusive_group(required=True);g.add_argument('--prepare',action='store_true');g.add_argument('--apply',action='store_true');parser.add_argument('--use-preflight',action='store_true');a=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'lock').open('w') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.prepare:prepare(a.use_preflight)
        else:run()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        if (OUT/'summary.json').exists():
            s=json.loads((OUT/'summary.json').read_text());s.update(status='stopped',error_type=type(exc).__name__,at=stamp());save('summary.json',s)
        raise
