#!/usr/bin/env python3
# поток: card
"""Authorized append-only supplier EAN13 update, night October 13–14."""
import argparse,csv,fcntl,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
from collections import defaultdict
import xml.etree.ElementTree as ET
import requests
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import ms_api
from prices import loader
from prices.profiles import get_identity
from tools import card_supplier_barcode_receipt_audit as audit
from tools.card_rapid_description_sync import log,stamp,window

SOURCE=ROOT/'docs/reports/card_ms_supplier_barcodes_check_2026-10-13'
OUT=ROOT/'docs/reports/card_supplier_ean13_sync_2026-10-13'
START=datetime(2026,10,13,21,30,tzinfo=timezone.utc)
CUTOFF=datetime(2026,10,14,2,tzinfo=timezone.utc)

def save(name,value):
    with (OUT/name).open('w') as f:
        f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n');f.flush();os.fsync(f.fileno())

def patch(live,row):
    if live.get('id')!=row['id'] or live.get('archived') or live.get('article')!=row['article'] or live.get('externalCode')!=row['external_code'] or ms_api.meta_id(live,'supplier')!=row['supplier_id']:
        raise RuntimeError('Card identity changed')
    old=live.get('barcodes') or []
    existing={str(b['ean13']) for b in old if b.get('ean13')}
    missing=[v for v in row['ean13'] if v not in existing]
    if not missing:return None
    other={str(v) for b in old for k,v in b.items() if k!='ean13'}
    if any(v in other for v in missing):raise RuntimeError('Supplier code already under another barcode type')
    return {'id':row['id'],'barcodes':old+[{'ean13':v} for v in missing]}

def prepare():
    if (OUT/'journal.jsonl').exists():raise RuntimeError('Prior journal exists')
    preview=json.loads((SOURCE/'preview_additions.json').read_text())
    positions=json.loads((SOURCE/'live_positions_expanded.json').read_text())
    cards={p['assortment']['id']:p['assortment'] for d in positions for p in d['positions']}
    assert len(preview)==len({r['id'] for r in preview})==3637
    plan=[];before=[]
    for r in preview:
        c=cards[r['id']];ean=[b['ean13'] for b in r['new_barcodes'][len(r['old_barcodes']):]]
        assert ean and all(len(v)==13 and audit.valid_gtin(v) for v in ean)
        row={'id':r['id'],'supplier_key':r['supplier'],'supplier_id':ms_api.meta_id(c,'supplier'),
             'article':r['article'],'external_code':r['external_code'],'ean13':ean,'old_barcodes':r['old_barcodes']}
        assert patch(c,row)['barcodes']==r['new_barcodes'];plan.append(row);before.append(c)
    plan.sort(key=lambda r:r['id']);save('plan.json',plan);save('ms_before.json',before)
    with (OUT/'plan.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(plan[0]),delimiter=';');w.writeheader();w.writerows(plan)
    save('summary.json',{'status':'scheduled_ready','at':stamp(),'authorized_candidates':3637,'changes':3637,'writes':0,'verified':0,
                        'plan_sha256':hashlib.sha256((OUT/'plan.json').read_bytes()).hexdigest(),
                        'start_msk':'2026-10-14 00:30','cutoff_msk':'2026-10-14 05:00',
                        'write_fields':['barcodes'],'preserve_existing_barcodes':True,'batch_size':50,'pause_seconds':60,'canary_count':200,'canary_pause_seconds':600})
    print('Frozen 3637 append-only EAN13 targets',flush=True)

def guard():
    waited=False
    while True:
        now=datetime.now(timezone.utc)
        if now<START or now>=CUTOFF or not window():raise RuntimeError('Outside authorized night')
        active=any(subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0 for unit in ['prc-enter-buyprice.service','card-solutionsprint-resolved-20261013.service'])
        if not active:return waited
        waited=True
        print(stamp(),'Waiting for existing mass writer',flush=True);time.sleep(30)

def source_and_receipt_gate(plan):
    folder=OUT/'supplier_preflight';folder.mkdir(exist_ok=True);audit.OUT=folder;audit.sources()
    indexes={k:defaultdict(dict) for k in ['rapid','profiline','bulat']}
    for r in json.loads((folder/'rapid.json').read_text())['price']:
        if audit.key(r.get('CodeID')):indexes['rapid'][audit.key(r['CodeID'])][str(r['ID'])]=(r['CodeID'],[r.get('Ean')])
    for r in json.loads((folder/'profiline.json').read_text())['products'].values():
        if audit.key(r.get('vendorCode')):indexes['profiline'][audit.key(r['vendorCode'])][r['code']]=(r['vendorCode'],[r.get('barCode')])
    for r in ET.fromstring((folder/'bulat.xml').read_bytes()).findall('.//offer'):
        for a in {r.get('id'),*(x.text for x in r.findall('vendorCode'))}:
            if audit.key(a):indexes['bulat'][audit.key(a)][r.get('id')]=(r.get('id'),[x.text for x in r.findall('ean13')])
    entered={k:set() for k in indexes}
    docs=[]
    for key in indexes:
        found=loader.existing_docs(get_identity(key));assert found
        prefix=max(d['name'].rsplit('_p',1)[0] for d in found)
        for d in [d for d in found if d['name'].startswith(prefix+'_p')]:
            data=ms_api.get('/entity/enter/'+d['id']+'/positions',{'limit':1000});assert data['meta']['size']<=1000
            entered[key].update(ms_api.meta_id(p,'assortment') for p in data['rows']);docs.append({'id':d['id'],'name':d['name'],'supplier_key':key})
    save('current_receipt_documents_before_write.json',docs)
    eligible=[];skipped=[]
    for row in plan:
        reason=''
        if row['id'] not in entered[row['supplier_key']]:reason='NO_LONGER_IN_CURRENT_RECEIPT'
        matches=list(indexes[row['supplier_key']].get(audit.key(row['article']),{}).values())
        if len(matches)>1:
            exact=[r for r in matches if str(r[0]).strip()==row['article'].strip()]
            if len(exact)==1:matches=exact
        if not reason and len(matches)!=1:reason='SOURCE_MAPPING_CHANGED'
        if not reason:
            codes={v for v in audit.tokens(matches[0][1]) if len(v)==13 and audit.valid_gtin(v)}
            if not set(row['ean13'])<=codes:reason='SOURCE_EAN13_CHANGED'
        if reason:skipped.append({'id':row['id'],'article':row['article'],'reason':reason})
        else:eligible.append(row)
    save('skipped_preflight.json',skipped);save('execution_plan.json',eligible);return eligible,skipped

def run():
    summary=json.loads((OUT/'summary.json').read_text())
    if summary['status']!='scheduled_ready' or (OUT/'journal.jsonl').exists():raise RuntimeError('Not a fresh frozen run')
    assert hashlib.sha256((OUT/'plan.json').read_bytes()).hexdigest()==summary['plan_sha256']
    guard();plan=json.loads((OUT/'plan.json').read_text());assert len(plan)==3637
    plan,skipped=source_and_receipt_gate(plan);summary.update(changes=len(plan),skipped_preflight=len(skipped),status='source_verified');save('summary.json',summary)
    canary_done=False
    for start in range(0,len(plan),50):
        guard();payload=[];before=[]
        for row in plan[start:start+50]:
            time.sleep(.2);live=ms_api.get('/entity/product/'+row['id']);body=patch(live,row)
            if body:payload.append(body);before.append(live)
            else:log(OUT,{'phase':'verified','id':row['id'],'already_desired':True});summary['verified']+=1
        if not payload:continue
        save(f'backup_batch_{start:05d}.json',before);guard()
        # Re-read after a competing-writer pause; retain all live codes, never overwrite a new addition.
        pending=[row for row in plan[start:start+50] if any(p['id']==row['id'] for p in payload)]
        while True:
            fresh=[];before=[];still_pending=[]
            for row in pending:
                time.sleep(.2);live=ms_api.get('/entity/product/'+row['id']);body=patch(live,row)
                if body:fresh.append(body);before.append(live);still_pending.append(row)
                else:log(OUT,{'phase':'verified','id':row['id'],'already_desired':True});summary['verified']+=1
            pending=still_pending;payload=fresh
            save(f'backup_batch_{start:05d}.json',before)
            if not guard():break
        if not payload:continue
        log(OUT,{'phase':'sending','ids':[r['id'] for r in payload]})
        begin=time.monotonic()
        try:response=requests.post(ms_api.BASE+'/entity/product',json=payload,headers={'Authorization':'Bearer '+ms_api.TOKEN},timeout=(15,120))
        except requests.RequestException:raise RuntimeError('Uncertain write; no automatic retry') from None
        if response.status_code!=200:raise RuntimeError('MS write HTTP '+str(response.status_code))
        answer=response.json()
        if not isinstance(answer,list) or {r.get('id') for r in answer}!={r['id'] for r in payload}:raise RuntimeError('Unexpected write response')
        elapsed=time.monotonic()-begin;summary['writes']+=len(payload)
        for r in payload:
            time.sleep(.2);live=ms_api.get('/entity/product/'+r['id'])
            if live.get('barcodes')!=r['barcodes']:raise RuntimeError('Barcode readback mismatch')
            log(OUT,{'phase':'verified','id':r['id']});summary['verified']+=1
        summary.update(status='running',at=stamp());save('summary.json',summary);print(stamp(),'verified',summary['verified'],'/',len(plan),flush=True)
        if summary['verified']>=200 and not canary_done:
            time.sleep(600);guard();begin=time.monotonic();ms_api.get('/entity/customerorder',{'limit':1})
            if time.monotonic()-begin>10:raise RuntimeError('Operational document read slow')
            canary_done=True
        if start+50<len(plan):time.sleep(120 if elapsed>10 else 60)
    if summary['verified']!=len(plan):raise RuntimeError('Final verified count mismatch')
    summary.update(status='complete',at=stamp());save('summary.json',summary)

def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--prepare',action='store_true');g.add_argument('--apply',action='store_true');a=p.parse_args();OUT.mkdir(exist_ok=True)
    with (OUT/'lock').open('w') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.prepare:prepare()
        else:run()
if __name__=='__main__':
    try:main()
    except Exception as e:
        if (OUT/'summary.json').exists():
            s=json.loads((OUT/'summary.json').read_text());s.update(status='stopped',error_type=type(e).__name__,at=stamp());save('summary.json',s)
        raise
