# поток: card
"""One-off authorized document exclusion, with live backups and verification."""
import sys, os, json, time, csv
from pathlib import Path
from copy import deepcopy
from collections import Counter
import requests
from dotenv import load_dotenv

ROOT = Path('/opt/mp-analytics')
OUT = ROOT / 'docs/reports/card_wb_documents_2026-10-13'
EXAMPLE = 1557593815
load_dotenv(ROOT / '.env')
SESSION = requests.Session()
SESSION.headers['Authorization'] = os.getenv('WB_TOKEN_CONTENT_ACC2') or os.environ['WB_TOKEN_ACC2']
BASE = 'https://content-api.wildberries.ru'
LAST = 0

def save(name, obj):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))

def call(path, body, write=False):
    global LAST
    time.sleep(max(0, (6.1 if write else .65) - (time.monotonic()-LAST)))
    LAST = time.monotonic()
    r = SESSION.post(BASE+path, json=body, timeout=60)
    if r.status_code == 429 and not write:
        time.sleep(max(6, int(r.headers.get('Retry-After', 10))))
        return call(path, body)
    r.raise_for_status()
    d = r.json()
    if d.get('error'): raise RuntimeError(str(d))
    return d

def fetch(nm):
    cs = call('/content/v2/get/cards/list', {'settings': {'cursor': {'limit':100}, 'filter': {'withPhoto':-1, 'textSearch':str(nm)}}}).get('cards', [])
    found = [c for c in cs if c['nmID']==nm]
    if len(found)!=1: raise RuntimeError(f'Exact identity missing: {nm}')
    return found[0]

def wanted(c):
    d = c.get('documents') or {}
    return d.get('overallVerdict', {}).get('reason')=='documents_missing' and d.get('overallVerdict', {}).get('status')==2 and d.get('excludeDocuments') is False

def scan():
    example = fetch(EXAMPLE)
    save('example_before.json', example)
    print('Example', EXAMPLE, example['vendorCode'], example.get('documents'), flush=True)
    assert wanted(example), 'Example differs from target error'
    cursor={'limit':100}; seen=set(); targets=[]; reasons=Counter(); pages=0
    while True:
        d=call('/content/v2/get/cards/list', {'settings':{'sort':{'ascending':True},'cursor':cursor,'filter':{'withPhoto':-1}}})
        save(f'catalog/page_{pages:04}.json',d)
        cs=d.get('cards',[])
        for c in cs:
            nm=c['nmID']
            if nm in seen: raise RuntimeError('Duplicate nmID in pagination')
            seen.add(nm)
            reasons[str((c.get('documents') or {}).get('overallVerdict',{}).get('reason'))]+=1
            if wanted(c): targets.append(c)
        pages+=1
        if pages%30==0: print('Scanned',len(seen),'targets',len(targets),flush=True)
        cur=d.get('cursor',{})
        if cur.get('total',len(cs))<100: break
        cursor={'limit':100,'updatedAt':cur['updatedAt'],'nmID':cur['nmID']}
    assert EXAMPLE in {c['nmID'] for c in targets}
    save('targets.json',targets)
    summary={'account':'wb_acc2','catalog_count':len(seen),'target_count':len(targets),'reason_counts':dict(reasons),'example_in_scope':True}
    save('scan_summary.json',summary)
    with (OUT/'targets.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['nmID','vendorCode','title','subjectName'],delimiter=';');w.writeheader()
        w.writerows({k:c.get(k) for k in w.fieldnames} for c in targets)
    print(summary,flush=True)

def blocked_scope():
    report=json.loads((OUT/'blocked_report.json').read_text())['report']
    assert len(report)==117 and any(r['nmId']==EXAMPLE for r in report)
    assert all('нет обязательных разрешительных документов' in r['reason'] for r in report)
    targets=[]; already=[]
    for row in report:
        c=fetch(row['nmId'])
        save(f'scope_before/{c["nmID"]}.json',c)
        assert c['vendorCode']==row['vendorCode'], f'Wrong identity {row["nmId"]}'
        if (c.get('documents') or {}).get('excludeDocuments') is True:
            already.append(c);continue
        assert wanted(c), f'Wrong target error {row["nmId"]}'
        assert not (c.get('documents') or {}).get('items'), 'Attached document exists'
        targets.append(c)
        if len(targets)%20==0: print('Confirmed blocked scope',len(targets),flush=True)
    save('targets.json',targets)
    save('already_set.json',already)
    save('scan_summary.json',{'account':'wb_acc2','source':'banned-products/blocked','blocked_count':len(report),'target_count':len(targets),'already_set_count':len(already),'example_in_scope':True,'subjects':dict(Counter(c['subjectName'] for c in targets))})
    with (OUT/'targets.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['nmID','vendorCode','title','subjectName'],delimiter=';');w.writeheader()
        w.writerows({k:c.get(k) for k in w.fieldnames} for c in targets)
    print('Confirmed blocked scope',len(report),'need update',len(targets),'already set',len(already),'subjects',dict(Counter(c['subjectName'] for c in targets)),flush=True)

def payload(c):
    p={k:deepcopy(c[k]) for k in ('nmID','vendorCode','brand','title','description','kizMarked','gtin','wholesale') if k in c}
    p['dimensions']={k:v for k,v in c['dimensions'].items() if k in ('length','width','height','weightBrutto')}
    p['sizes']=[{k:v for k,v in s.items() if k in ('chrtID','techSize','wbSize','skus')} for s in c['sizes']]
    p['characteristics']=[{'id':a['id'],'value':deepcopy(a['value'])} for a in c['characteristics']]
    assert not (c.get('documents') or {}).get('items'), 'Attached documents: stop to avoid deletion'
    p['documents']={'items':[],'excludeDocuments':True}
    return p

def preserved(c):
    c=deepcopy(c);c.pop('updatedAt',None);c.pop('documents',None)
    c.get('dimensions',{}).pop('isValid',None)
    c['characteristics']={str(a['id']):a['value'] for a in c.get('characteristics',[])}
    return c

def apply():
    targets=json.loads((OUT/'targets.json').read_text())
    report=json.loads((OUT/'blocked_report.json').read_text())['report']
    already=json.loads((OUT/'already_set.json').read_text())
    assert len(targets)+len(already)==117 and {c['nmID'] for c in targets+already}=={r['nmId'] for r in report}, 'Scope differs from confirmed blocked report'
    targets.sort(key=lambda c:c['nmID']!=EXAMPLE)
    results=[]
    batches=[targets[:1]]+[targets[i:i+20] for i in range(1,len(targets),20)]
    for batch in batches:
        ready=[]
        sent=(OUT/f'responses/{batch[0]["nmID"]}.json').exists()
        for old in batch:
            c=fetch(old['nmID'])
            assert c['vendorCode']==old['vendorCode'], 'Identity changed'
            if sent:
                before=json.loads((OUT/f'before/{c["nmID"]}.json').read_text())
                assert preserved(before)==preserved(c), 'Content changed while request pending'
                ready.append((before,payload(before)));continue
            assert wanted(c), 'Error changed'
            p=payload(c);save(f'before/{c["nmID"]}.json',c);save(f'requests/{c["nmID"]}.json',p)
            ready.append((c,p))
        if not sent:
            save(f'sending/{ready[0][0]["nmID"]}.json',{'at':time.time(),'nmIDs':[c['nmID'] for c,p in ready]})
            response=call('/content/v2/cards/update',[p for c,p in ready],write=True)
            save(f'responses/{ready[0][0]["nmID"]}.json',response)
        time.sleep(4)
        for before,p in ready:
            after=None
            sent_at=(OUT/f'responses/{batch[0]["nmID"]}.json').stat().st_mtime
            deadline=time.monotonic()+max(0,1800-(time.time()-sent_at))
            for attempt in range(65):
                if attempt: time.sleep(30)
                after=fetch(before['nmID'])
                if (after.get('documents') or {}).get('excludeDocuments') is True: break
                if attempt%2==0: print('Waiting for WB synchronization',before['nmID'],'attempt',attempt,flush=True)
                if time.monotonic()>=deadline: break
            save(f'after/{before["nmID"]}.json',after)
            assert (after.get('documents') or {}).get('excludeDocuments') is True, 'Flag not saved'
            assert preserved(before)==preserved(after), f'Unexpected content change {before["nmID"]}'
            results.append({'nmID':before['nmID'],'vendorCode':before['vendorCode'],'flag_verified':True,'content_preserved':True,'verdict':after.get('documents',{}).get('overallVerdict')})
            save('results.json',results)
            save('run_status.json',{'status':'RUNNING','verified':len(results),'total':len(targets),'at':time.time()})
        print('Verified',len(results),'of',len(targets),flush=True)
    save('apply_summary.json',{'count':len(results),'flag_verified':len(results),'content_preserved':len(results),'verdicts':dict(Counter(str(r['verdict']) for r in results))})
    save('run_status.json',{'status':'COMPLETE','verified':len(results),'already_set':len(already),'total':len(targets),'at':time.time()})

def submit_remaining():
    # Direct instruction 13.10: submit the other 115 despite unresolved pilot.
    targets=json.loads((OUT/'targets.json').read_text())
    report=json.loads((OUT/'blocked_report.json').read_text())['report']
    already=json.loads((OUT/'already_set.json').read_text())
    assert len(targets)==116 and len(already)==1
    assert {c['nmID'] for c in targets+already}=={r['nmId'] for r in report}
    assert (OUT/f'responses/{EXAMPLE}.json').exists(), 'Pilot response missing'
    targets=[c for c in targets if c['nmID']!=EXAMPLE]
    assert len(targets)==115
    results=[]
    for start in range(0,115,20):
        batch=targets[start:start+20]; ready=[]
        response_name=f'remaining/responses/{batch[0]["nmID"]}.json'
        sent=(OUT/response_name).exists()
        for old in batch:
            c=fetch(old['nmID'])
            assert c['vendorCode']==old['vendorCode'], 'Identity changed'
            if sent:
                before=json.loads((OUT/f'before/{c["nmID"]}.json').read_text())
                ready.append((before,payload(before)));continue
            assert wanted(c) or (c.get('documents') or {}).get('excludeDocuments') is True, 'Error changed'
            p=payload(c)
            save(f'before/{c["nmID"]}.json',c)
            save(f'requests/{c["nmID"]}.json',p)
            ready.append((c,p))
        if not sent:
            save(f'remaining/sending/{batch[0]["nmID"]}.json',{'at':time.time(),'nmIDs':[c['nmID'] for c,p in ready]})
            response=call('/content/v2/cards/update',[p for c,p in ready],write=True)
            save(response_name,response)
        for before,p in ready:
            results.append({'nmID':before['nmID'],'vendorCode':before['vendorCode'],'api_accepted':True})
        save('remaining_submission_results.json',results)
        save('run_status.json',{'status':'SUBMITTING_REMAINING','remaining_accepted':len(results),'pilot_accepted':1,'at':time.time()})
        print('API accepted remaining',len(results),'of 115',flush=True)
    observations=[]
    for old in [json.loads((OUT/'example_before.json').read_text())]+targets:
        c=fetch(old['nmID']);save(f'after/{c["nmID"]}.json',c)
        before=json.loads((OUT/f'before/{c["nmID"]}.json').read_text())
        ok=preserved(before)==preserved(c)
        observations.append({'nmID':c['nmID'],'excludeDocuments':c.get('documents',{}).get('excludeDocuments'),'content_preserved':ok,'verdict':c.get('documents',{}).get('overallVerdict')})
        save('remaining_observations.json',observations)
        assert ok, f'Unexpected content change {c["nmID"]}'
        if len(observations)%20==0: print('Read-back',len(observations),flush=True)
    summary={'status':'SUBMITTED','api_accepted':116,'remaining_accepted':115,'already_set_before':1,'readback_count':len(observations),'flag_true':sum(r['excludeDocuments'] is True for r in observations),'flag_false':sum(r['excludeDocuments'] is False for r in observations),'content_preserved':sum(r['content_preserved'] for r in observations),'at':time.time()}
    save('run_status.json',summary);save('submission_summary.json',summary)
    print(summary,flush=True)

if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    try:
        {'scan':scan,'blocked_scope':blocked_scope,'apply':apply,'submit_remaining':submit_remaining}[sys.argv[1]]()
    except Exception as exc:
        if sys.argv[1] in ('apply','submit_remaining'):
            f=OUT/'results.json'
            save('run_status.json',{'status':'STOPPED','reason':str(exc),'verified':len(json.loads(f.read_text())) if f.exists() else 0,'at':time.time()})
        raise
