#!/usr/bin/env python3
# поток: card
"""Fill only the empty TN VED field of the exact eleven authorized cards."""
import argparse
from collections import Counter
import csv
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import requests
from collectors.wb import _content_token
from tools.card_wb_tnved_rule_recheck import fetch,char
from tools.card_wb_tnved_complete_skipped_23 import payload_for_partial
from tools.card_wb_tnved_apply_203 import preservation

SOURCE=ROOT/'docs/reports/wb_tnved_final_catalog_audit_2026-10-05/missing_fields.csv'
OUT=ROOT/'docs/reports/wb_tnved_complete_11_2026-10-05'
SCOPE_HASH='2f7182512d604787d7acf276b66f22359b4554cd68f7960235fa77dfd98e6fbc'
FIELDS=(15000001,15004139)

def now():return datetime.now(timezone.utc).isoformat()

def save(name,value):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    with tmp.open('w') as h:
        json.dump(value,h,ensure_ascii=False,indent=2);h.flush();os.fsync(h.fileno())
    os.replace(tmp,p)

def journal(value):
    with (OUT/'journal.jsonl').open('a') as h:
        h.write(json.dumps({'at':now(),**value},ensure_ascii=False)+'\n');h.flush();os.fsync(h.fileno())

def scope():
    rows=[r for r in csv.DictReader(SOURCE.open(encoding='utf-8-sig'),delimiter=';')
          if r['status']=='ONE_EMPTY' and r['held_by_owner']=='False']
    ids=sorted((r['account'],int(r['nmID']),r['vendorCode']) for r in rows)
    digest=hashlib.sha256(json.dumps(ids,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if len(rows)!=11 or digest!=SCOPE_HASH:raise ValueError('Authorized scope changed')
    return rows

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True);rows=scope()
    # An uncertain mutation is resolved by reading the saved before/after evidence,
    # never by automatically running this script a second time.
    if args.apply and (OUT/'journal.jsonl').exists():raise ValueError('Existing mutation journal: verify before any rerun')
    save('authorization.json',{'at':now(),'instruction':'дозаполни 11 карточек где одно поле пустое',
        'scope_sha256':SCOPE_HASH,'scope':[(r['account'],int(r['nmID']),r['vendorCode']) for r in rows],
        'preserve_existing_field':True,'mode':'apply' if args.apply else 'preview'})
    results={};observations=[];writes=0
    with requests.Session() as session:
        allowed={}
        for sid in (1820,1907):
            response=session.get(f'https://content-api.wildberries.ru/content/v2/directory/tnved?subjectID={sid}',
                headers={'Authorization':_content_token('wb_acc1')},timeout=45)
            response.raise_for_status();data=response.json();save(f'dictionary_{sid}.json',data)
            allowed[sid]={str(x['tnved']) for x in data['data']};time.sleep(.7)
        for account in ('wb_acc1','wb_acc2'):
            ready=[]
            for row in [r for r in rows if r['account']==account]:
                nm=int(row['nmID']);key=f'{account}:{nm}'
                before=fetch(session,account,nm)
                if not before or before['vendorCode']!=row['vendorCode']:raise ValueError('Card identity changed')
                if before['subjectID']!=int(row['subjectID']):raise ValueError('Subject changed since audit')
                first,second=[char(before,f) for f in FIELDS];filled=[v for v in (first,second) if v]
                if len(filled)==2:
                    results[key]={'status':'ALREADY_BOTH_FILLED','fields':[first,second]};continue
                if len(filled)!=1:raise ValueError('No longer exactly one field empty')
                code=filled[0]
                if code not in allowed.get(before['subjectID'],set()):raise ValueError('Existing code not allowed for WB subject')
                expected='8443999000' if before['subjectID']==1820 else '3215900009' if before['subjectID']==1907 else None
                if code!=expected:raise ValueError('Code outside owner-approved rule')
                payload=payload_for_partial(before,code)
                save(f'before/{key}.json',before)
                observations.append({'account':account,'nmID':nm,'vendorCode':row['vendorCode'],'before_fields':[first,second],'code':code})
                ready.append((row,before,code,payload))
            save('preview.json',observations)
            print(account,'ready',len(ready),flush=True)
            if not args.apply or not ready:continue
            body=[r[3] for r in ready];save(f'request_{account}.json',body)
            journal({'phase':'intent','account':account,'nmIDs':[int(r[0]['nmID']) for r in ready]})
            response=session.post('https://content-api.wildberries.ru/content/v2/cards/update',
                headers={'Authorization':_content_token(account)},json=body,timeout=45)
            answer=response.json() if response.content else {};save(f'response_{account}.json',{'http':response.status_code,'body':answer})
            journal({'phase':'response','account':account,'http':response.status_code,'body':answer})
            response.raise_for_status()
            if answer.get('error'):raise RuntimeError('WB rejected update; do not resend')
            writes+=1;time.sleep(6.1)
            for row,before,code,_ in ready:
                nm=int(row['nmID']);key=f'{account}:{nm}';deadline=time.monotonic()+120
                while True:
                    after=fetch(session,account,nm)
                    if after and all(char(after,f)==code for f in FIELDS):
                        save(f'after/{key}.json',after)
                        if preservation(before)!=preservation(after):raise ValueError('Other content changed; see before/after')
                        for f in FIELDS:
                            if char(before,f):
                                old=next(c['value'] for c in before['characteristics'] if c['id']==f)
                                new=next(c['value'] for c in after['characteristics'] if c['id']==f)
                                if new!=old:raise ValueError('Originally filled value changed')
                        results[key]={'status':'VERIFIED','code':code,'at':now(),'vendorCode':row['vendorCode']}
                        save('results.json',results);journal({'phase':'verified','account':account,'nmID':nm});break
                    if time.monotonic()>deadline:raise RuntimeError('Result unverified; no automatic resend')
                    time.sleep(4)
        summary={'at':now(),'scope':11,'mode':'apply' if args.apply else 'preview','counts':dict(Counter(r['status'] for r in results.values())),
                 'write_requests':writes,'originally_filled_fields_preserved':True,'all_other_content_preserved':True}
        save('summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
