#!/usr/bin/env python3
# поток: card
"""Owner-authorized completion of the empty TN VED field in the skipped 23."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
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
from tools.card_wb_tnved_remaining import scope
from tools.card_wb_tnved_rule_recheck import fetch, char
from tools.card_wb_tnved_dryrun import pilot_request
from tools.card_wb_tnved_apply_203 import preservation

OUT=ROOT/'docs/reports/wb_tnved_complete_skipped_23_2026-10-05'
FIELDS=(15000001,15004139)

def save(name,data):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

def journal(data):
    with (OUT/'journal.jsonl').open('a') as h:
        h.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),**data},ensure_ascii=False)+'\n')
        h.flush();os.fsync(h.fileno())

def targets():
    state=json.loads((ROOT/'docs/reports/wb_tnved_remaining_2026-10-05/state.json').read_text())
    bounded=scope();result=[]
    for key,value in state['results'].items():
        if value['status']!='ALREADY_FILLED':continue
        account,nm=key.split(':');result.append(bounded[(account,int(nm))])
    assert len(result)==23 and all(r['account']=='wb_acc2' for r in result)
    assert all(r['vendorCode']!='4434UU1ZP70G' for r in result)
    return sorted(result,key=lambda r:r['nmID'])

def payload_for_partial(card,code):
    existing={c['id']:deepcopy(c) for c in card['characteristics'] if c['id'] in FIELDS and c.get('value')}
    if len(existing)!=1:raise ValueError('Requires exactly one filled field')
    copy=deepcopy(card)
    copy['characteristics']=[c for c in copy['characteristics'] if c['id'] not in FIELDS]
    payload=pilot_request(copy,code)[0]
    payload['characteristics']=[{'id':c['id'],'value':deepcopy(existing[c['id']]['value'])} if c['id'] in existing else c
                                for c in payload['characteristics']]
    docs=card.get('documents')
    if docs is not None:
        if docs.get('items'):raise ValueError('Attached documents need preservation mapping')
        payload['documents']={'items':[],'excludeDocuments':docs.get('excludeDocuments',False)}
    return payload

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True);rows=targets();observations=[];ready=[]
    save('authorization.json',{'instruction':'Им надо дозаполнить второе поле','scope':rows,
        'scope_hash':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
        'preserve_existing_code':True,'mode':'apply' if args.apply else 'preview'})
    with requests.Session() as session:
        allowed={}
        for sid in (1820,1907):
            r=session.get(f'https://content-api.wildberries.ru/content/v2/directory/tnved?subjectID={sid}',
                          headers={'Authorization':_content_token('wb_acc2')},timeout=45)
            r.raise_for_status();d=r.json();save(f'dictionary_{sid}.json',d)
            allowed[sid]={str(x['tnved']) for x in d['data']};time.sleep(.7)
        for row in rows:
            card=fetch(session,row['account'],row['nmID'])
            if not card or card['vendorCode']!=row['vendorCode']:raise ValueError('Identity not confirmed')
            values={str(field):char(card,field) for field in FIELDS};filled=[v for v in values.values() if v]
            status='ALREADY_BOTH_FILLED' if len(filled)==2 else 'READY' if len(filled)==1 else 'REVIEW_BOTH_EMPTY'
            expected='8443999000' if card['subjectID']==1820 else row['ink_code'] if card['subjectID']==1907 else None
            if status=='READY' and (filled[0]!=expected or filled[0] not in allowed.get(card['subjectID'],set())):
                status='REVIEW_EXISTING_CODE'
            observations.append({**row,'subjectID':card['subjectID'],'fields':values,'status':status})
            save(f'before/{row["nmID"]}.json',card)
            if status=='READY':ready.append((row,card,filled[0],payload_for_partial(card,filled[0])))
        save('preview.json',observations)
        print('preview',dict(Counter(r['status'] for r in observations)),flush=True)
        if not args.apply:return
        results={str(r['nmID']):{'status':r['status'],'fields':r['fields']} for r in observations}
        if ready:
            payload=[item[3] for item in ready];save('request.json',payload)
            journal({'phase':'intent','account':'wb_acc2','nmIDs':[item[0]['nmID'] for item in ready],'count':len(ready)})
            response=session.post('https://content-api.wildberries.ru/content/v2/cards/update',
                   headers={'Authorization':_content_token('wb_acc2')},json=payload,timeout=45)
            data=response.json() if response.content else {};save('response.json',{'http':response.status_code,'body':data})
            journal({'phase':'response','http':response.status_code,'body':data})
            response.raise_for_status()
            if data.get('error'):raise RuntimeError('WB rejected update; no automatic resend')
            time.sleep(6.1)
            for row,before,code,_ in ready:
                deadline=time.monotonic()+120
                while True:
                    after=fetch(session,row['account'],row['nmID'])
                    if after and all(char(after,f)==code for f in FIELDS):
                        if preservation(before)!=preservation(after):
                            save('preservation_failure.json',{'before':before,'after':after});raise ValueError('Other content changed')
                        for field in FIELDS:
                            if char(before,field):
                                original=next(c['value'] for c in before['characteristics'] if c['id']==field)
                                actual=next(c['value'] for c in after['characteristics'] if c['id']==field)
                                if actual!=original:raise ValueError('Previously filled field changed')
                        save(f'after/{row["nmID"]}.json',after);results[str(row['nmID'])]={'status':'VERIFIED','code':code}
                        save('results.json',results);journal({'phase':'verified','nmID':row['nmID']});break
                    if time.monotonic()>deadline:raise RuntimeError('Update unverified; do not resend')
                    time.sleep(4)
        save('results.json',results)
        summary={'at':datetime.now(timezone.utc).isoformat(),'scope':23,'counts':dict(Counter(r['status'] for r in results.values())),
            'write_requests':int(bool(ready)),'written':len(ready),'preserved_existing_values':True}
        save('summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
