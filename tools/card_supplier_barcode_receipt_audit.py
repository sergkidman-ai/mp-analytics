#!/usr/bin/env python3
# поток: card
"""Read-only supplier barcode coverage for current MS receipt products."""
import csv
import json
import os
import re
import sys
import time
import threading
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
import requests
import xml.etree.ElementTree as ET
from lxml import html

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import ms_api
from prices import loader
from prices.profiles import get_identity
from tools.card_solutionsprint_scraper import login,parse_card,text
from tools.card_solutionsprint_description_audit import fetch
OUT=ROOT/'docs/reports/card_supplier_barcode_receipt_audit_2026-10-13'
PROFILE_SOURCE={'rapid':'rapid','bulat':'bulat','profiline':'profiline','s_print_msk':'solutionsprint','s_print_spb':'solutionsprint'}

def save(name,d):
    (OUT/name).write_text(json.dumps(d,ensure_ascii=False,indent=2,default=str)+'\n')

def safe_get(url,params=None):
    try:r=requests.get(url,params=params,timeout=(15,90))
    except requests.RequestException:raise RuntimeError('Supplier source unavailable') from None
    if r.status_code!=200:raise RuntimeError('Supplier HTTP '+str(r.status_code))
    return r

def sources():
    def rapid():
        token=os.environ['RAPID_API_AUTHKEY'];r=safe_get('https://b2b-rapid1.ru/api/export.php',{'authkey':token,'type':'json'})
        data=r.json();assert data['error']['code']==0 and data['price']
        (OUT/'rapid.json').write_text(json.dumps(data,ensure_ascii=False).replace(token,'[REDACTED]'))
    def profiline():
        token=os.environ['PROFILINE_API_TOKEN'];r=safe_get('https://rm-profiline.online/json/hannover/client763/wms5/brand0/',{'token':token})
        data=r.json();assert data['products'];(OUT/'profiline.json').write_text(json.dumps(data,ensure_ascii=False).replace(token,'[REDACTED]'))
    def bulat():
        r=safe_get('https://bulat-group.ru/price/clientapi.xml');assert ET.fromstring(r.content).tag=='yml_catalog';(OUT/'bulat.xml').write_bytes(r.content)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for f in pool.map(lambda f:f(),[rapid,profiline,bulat]):pass
    save('source_collection.json',{'at_utc':datetime.now(timezone.utc).isoformat()})
    print('Fresh supplier sources collected',flush=True)

def receipts():
    docs=[]
    for key in PROFILE_SOURCE:
        found=loader.existing_docs(get_identity(key))
        assert found
        party=max(d['name'].rsplit('_p',1)[0] for d in found)
        docs.extend({**d,'source_key':PROFILE_SOURCE[key],'profile_key':key} for d in found if d['name'].startswith(party+'_p'))
    save('current_docs.json',docs)
    def positions(d):
        r=ms_api.get('/entity/enter/'+d['id']+'/positions',{'limit':1000,'expand':'assortment'})
        assert r['meta']['size']<=1000
        assert all('id' in p['assortment'] for p in r['rows'])
        return {'id':d['id'],'name':d['name'],'source_key':d['source_key'],'profile_key':d['profile_key'],'positions':r['rows']}
    with ThreadPoolExecutor(max_workers=3) as pool:result=list(pool.map(positions,docs))
    save('current_positions_expanded.json',result)
    print('Current expanded receipt documents '+str(len(result)),flush=True)

def cards_scope():
    rows=json.loads((OUT/'current_positions_expanded.json').read_text());scopes=defaultdict(dict)
    for doc in rows:
        for p in doc['positions']:
            c=p['assortment'];scopes[doc['source_key']][c['id']]=c
    return scopes

def sprint():
    scopes=cards_scope()['solutionsprint'];wanted=set()
    for c in scopes.values():
        if c.get('archived'):continue
        m=re.fullmatch(r'SP(\d+)(?:_MSK)?',c.get('article')or'',re.I)
        if m:wanted.add(m[1])
    old=ROOT/'docs/reports/card_solutionsprint_current_review_2026-10-12/source_listings.json'
    index=defaultdict(dict)
    for r in json.loads(old.read_text()):index[r['supplier_code']][r['url']]=r
    local=threading.local();lock=threading.Lock();next_start=[0.0]
    def doc(url,params=None):
        if not hasattr(local,'session'):local.session=requests.Session();login(local.session)
        with lock:
            time.sleep(max(0,next_start[0]-time.monotonic()));next_start[0]=time.monotonic()+.5
        return fetch(local.session,url,params)
    def one(code):
        cache=OUT/'sprint_pages'/f'{code}.json'
        if cache.exists():return json.loads(cache.read_text())
        found=list(index.get(code,{}).values())
        if len(found)!=1:
            from tools.card_solutionsprint_description_audit import listing
            found=[r for r in listing(doc('/search/index.php',{'q':code,'s':'Поиск'})) if r['supplier_code']==code]
        if len(found)!=1:return {'supplier_code':code,'status':'NO_UNIQUE_CURRENT_URL'}
        try:
            page=doc(found[0]['url']);d=parse_card(html.tostring(page),found[0]['url'],authenticated=True)
        except (RuntimeError,ValueError):return {'supplier_code':code,'status':'PAGE_UNAVAILABLE'}
        assert d['supplier_code']==code
        values={k:v for k,v in d['properties'].items() if re.search(r'штрих|bar.?code|\bean(?:13|8)?\b|\bgtin\b',k,re.I)}
        main=page.xpath('//*[@data-entity="main-product-wrap"]')[0]
        labelled=re.findall(r'(?:штрих\s*код|barcode|ean13|gtin)\s*[:=]\s*([0-9][0-9 ,;|]{6,100})',text(main),re.I)
        result={'supplier_code':code,'status':'READ_OK','url':found[0]['url'],'name':d['name'],
                'barcode_fields':values,'labelled_barcode_values':labelled,'properties_labels':list(d['properties'])}
        cache.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result
    result=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(one,c):c for c in sorted(wanted)}
        for task in as_completed(tasks):
            result.append(task.result())
            if len(result)%100==0 or len(result)==len(tasks):
                save('sprint_progress.json',{'done':len(result),'total':len(tasks),'at':datetime.now(timezone.utc).isoformat()})
                print('Sprint barcode pages '+str(len(result))+'/'+str(len(tasks)),flush=True)
    save('sprint_product_details.json',result)

def key(v):return re.sub(r'\s+','',str(v or '')).upper()

def tokens(values):
    result=[]
    for v in values:
        if v is not None:
            # Excel's leading apostrophe is a text marker, not a barcode character.
            result.extend(x.strip("'\"") for x in re.split(r'[,;|\s]+',str(v).strip()) if x.strip("'\""))
    return list(dict.fromkeys(result))

def valid_gtin(value):
    if not value.isdigit() or len(value) not in (8,12,13,14):return False
    return (10-sum(int(d)*(3 if i%2==0 else 1) for i,d in enumerate(value[-2::-1]))%10)%10==int(value[-1])

def analyse():
    scopes=cards_scope();indexes={k:defaultdict(dict) for k in scopes}
    for r in json.loads((OUT/'rapid.json').read_text())['price']:
        if key(r.get('CodeID')):indexes['rapid'][key(r['CodeID'])][str(r['ID'])]={'article':r['CodeID'],'name':r['Name'],'barcodes':[r.get('Ean')],'field':'Ean'}
    for r in json.loads((OUT/'profiline.json').read_text())['products'].values():
        if key(r.get('vendorCode')):indexes['profiline'][key(r['vendorCode'])][r['code']]={'article':r['vendorCode'],'name':r['name'],'barcodes':[r.get('barCode')],'field':'barCode'}
    for r in ET.fromstring((OUT/'bulat.xml').read_bytes()).findall('.//offer'):
        row={'article':r.get('id'),'name':r.findtext('name'),'barcodes':[x.text for x in r.findall('ean13')],'field':'ean13'}
        for a in {r.get('id'),*(x.text for x in r.findall('vendorCode'))}:
            if key(a):indexes['bulat'][key(a)][r.get('id')]=row
    for r in json.loads((OUT/'sprint_product_details.json').read_text()):
        if r['status']=='READ_OK':indexes['solutionsprint'][r['supplier_code']][r['supplier_code']]={'article':r['supplier_code'],'name':r['name'],'barcodes':list(r['barcode_fields'].values())+r['labelled_barcode_values'],'field':';'.join(r['barcode_fields']) or 'NO_FIELD'}
    review=[];summary={}
    for supplier,cards in scopes.items():
        selected=[c for c in cards.values() if not c.get('archived') and c.get('meta',{}).get('type')=='product']
        for c in selected:
            lookup=key(c.get('article'))
            if supplier=='solutionsprint':
                m=re.fullmatch(r'SP(\d+)(?:_MSK)?',c.get('article')or'',re.I);lookup=m[1] if m else ''
            found=list(indexes[supplier].get(lookup,{}).values())
            if len(found)>1:
                exact=[r for r in found if str(r['article']).strip()==str(c.get('article')or'').strip()]
                if len(exact)==1:found=exact
            r={'supplier':supplier,'id':c['id'],'external_code':c.get('externalCode'),'article':c.get('article'),
               'ms_name':c['name'],'source_match':len(found)==1,'source_candidates':len(found),'field':'',
               'raw_values':[],'tokens':[],'valid_gtins':[],'valid_ean13':[],'ms_barcodes':c.get('barcodes',[])}
            if len(found)==1:
                o=found[0];vals=[v for v in o['barcodes'] if v is not None and str(v).strip()];tok=tokens(vals)
                good=[v for v in tok if valid_gtin(v)]
                r.update(field=o['field'],raw_values=vals,tokens=tok,valid_gtins=good,valid_ean13=[v for v in good if len(v)==13])
            review.append(r)
        subset=[r for r in review if r['supplier']==supplier]
        summary[supplier]={'current_receipt_cards':len(selected),'archived_excluded':sum(bool(c.get('archived')) for c in cards.values()),
                           'matched_source_cards':sum(r['source_match'] for r in subset),'with_nonempty_barcode':sum(bool(r['raw_values']) for r in subset),
                           'with_valid_gtin':sum(bool(r['valid_gtins']) for r in subset),'with_valid_ean13':sum(bool(r['valid_ean13']) for r in subset),
                           'multiple_tokens_cards':sum(len(r['tokens'])>1 for r in subset),'token_format_counts':dict(Counter(('GTIN'+str(len(v)) if valid_gtin(v) else 'DIGITS_'+str(len(v))+'_INVALID_CHECKSUM' if v.isdigit() else 'NON_NUMERIC') for r in subset for v in r['tokens']))}
    save('review.json',review);save('summary.json',{'at_utc':datetime.now(timezone.utc).isoformat(),'suppliers':summary,'external_writes':0,'database_writes':0})
    with (OUT/'receipt_supplier_barcodes.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(review[0]),delimiter=';');w.writeheader();w.writerows(review)
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':
    OUT.mkdir(exist_ok=True);(OUT/'sprint_pages').mkdir(exist_ok=True)
    if '--saved' not in sys.argv:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(sources),pool.submit(receipts)]
            for f in futures:f.result()
        sprint()
    analyse()
