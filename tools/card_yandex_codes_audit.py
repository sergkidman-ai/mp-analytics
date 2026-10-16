#!/usr/bin/env python3
# поток: card
"""Fresh read-only Market commodity-code audit; writes reports only."""
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import requests
from core.db import query

API='https://api.partner.market.yandex.ru'
OUT=ROOT/'docs/reports/yandex_codes_audit_2026-10-05'

def now(): return datetime.now(timezone.utc).isoformat()

def save(name,data):
    p=OUT/name; p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,default=str)+'\n')

def read(session,path,body=None,params=None):
    if path!='/v2/campaigns' and not re.fullmatch(r'/v2/businesses/\d+/offer-mappings',path):
        raise ValueError('Endpoint is outside read-only allowlist')
    for attempt in range(4):
        r=session.get(API+path,params=params,timeout=45) if body is None else session.post(API+path,json=body,params=params,timeout=45)
        time.sleep(.7)
        if r.status_code in (420,429) or r.status_code>=500:
            time.sleep(min(35,5*(attempt+1))); continue
        r.raise_for_status(); d=r.json()
        if d.get('status')=='ERROR' or d.get('errors'): raise RuntimeError('Read rejected; no mutations attempted')
        return d
    raise RuntimeError(f'Read retries exhausted: HTTP {r.status_code}')

def csv_save(name,rows,columns):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=columns,delimiter=';');w.writeheader();w.writerows(rows)

def code_row(account,business,om):
    offer=om['offer']; mapping=om.get('mapping',{})
    codes=offer.get('commodityCodes') or []
    def values(typ):
        return sorted({str(c.get('code','')).strip() for c in codes if c.get('type')==typ and str(c.get('code','')).strip()})
    tn=values('CUSTOMS_COMMODITY_CODE'); ok=values('OKPD2_CODE')
    legacy=str(offer.get('customsCommodityCode') or '').strip()
    effective=tn or ([legacy] if legacy else [])
    status='BOTH_FILLED' if effective and ok else 'BOTH_EMPTY' if not effective and not ok else 'MISSING_OKPD2' if not ok else 'MISSING_TNVED'
    checks=[]
    if tn and legacy and legacy not in tn: checks.append('NEW_LEGACY_TNVED_CONFLICT')
    if len(tn)>1 or len(ok)>1: checks.append('MULTIPLE_CODES_OF_TYPE')
    if any(not re.fullmatch(r'(?:\d{10}|\d{14})',v) for v in effective): checks.append('TNVED_FORMAT')
    if any(not re.fullmatch(r'\d{2}(?:\.\d|\.\d{2}|\.\d{2}\.\d|\.\d{2}\.\d{2}|\.\d{2}\.\d{2}\.\d{3})?',v) for v in ok): checks.append('OKPD2_FORMAT')
    return {'account':account,'businessId':business,'offerId':str(offer['offerId']),
            'vendorCode':offer.get('vendorCode',''),'title':offer.get('name') or mapping.get('marketSkuName',''),
            'archived':bool(offer.get('archived',False)),'cardStatus':offer.get('cardStatus',''),
            'marketSku':mapping.get('marketSku',''),'category':mapping.get('marketCategoryName',''),
            'tnved':'; '.join(effective),'tnved_new':'; '.join(tn),'tnved_legacy':legacy,
            'okpd2':'; '.join(ok),'status':status,'review':'; '.join(checks)}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    start=now(); observations={}; scans=[]; campaigns=[]
    credentials=[]
    for i in (1,2):
        key=os.getenv(f'YANDEX_API_KEY_ACC{i}');biz=os.getenv(f'YANDEX_BUSINESS_ID_ACC{i}')
        if key and biz: credentials.append((f'ya_acc{i}',key,str(biz)))
    if not credentials: raise ValueError('No configured Market credentials')
    for account,key,configured_biz in credentials:
        with requests.Session() as session:
            session.headers.update({'Api-Key':key,'Content-Type':'application/json'})
            campaign_data=read(session,'/v2/campaigns');save(f'raw/{account}_campaigns.json',campaign_data)
            campaigns += campaign_data.get('campaigns',[])
            if campaign_data.get('paging',{}).get('nextPageToken'):
                raise ValueError('Campaign discovery needs pagination')
            businesses={configured_biz}|{str(c['business']['id']) for c in campaign_data.get('campaigns',[]) if c.get('business',{}).get('id')}
            for business in sorted(businesses):
                for archived in (False,True):
                    token=None; seen=set(); scan={'account':account,'businessId':business,'archived':archived,'pages':0,'observations':0,'exhausted':False}
                    for index in range(1,501):
                        params={'limit':100}
                        if token: params['pageToken']=token
                        data=read(session,f'/v2/businesses/{business}/offer-mappings',body={'archived':archived},params=params)
                        save(f'raw/{account}_{business}_{"archive" if archived else "active"}_{index:04d}.json',data)
                        result=data.get('result',{})
                        cards=result.get('offerMappings')
                        if not isinstance(cards,list): raise ValueError('Missing offerMappings')
                        for om in cards:
                            offer=om.get('offer',{});ident=str(offer.get('offerId',''))
                            if not ident: raise ValueError('Missing offer identity')
                            if bool(offer.get('archived',False)) != archived: raise ValueError('Archive filter mismatch')
                            observations[(account,business,ident)]=code_row(account,business,om)
                        scan['pages']+=1;scan['observations']+=len(cards)
                        token=result.get('paging',{}).get('nextPageToken')
                        if index%10==0: print(now(),account,'archive' if archived else 'active','pages',index,'observed',scan['observations'],flush=True)
                        if not token: scan['exhausted']=True;break
                        if token in seen: raise ValueError('Repeated pagination token')
                        seen.add(token)
                    if not scan['exhausted']: raise ValueError('Catalog scan not exhausted')
                    scans.append(scan);save('scan_progress.json',scans)
    rows=sorted(observations.values(),key=lambda r:(r['account'],r['businessId'],r['offerId']))
    if not rows: raise ValueError('Empty Market catalog')
    columns=list(rows[0]);csv_save('all_offers.csv',rows,columns)
    missing=[r for r in rows if r['status']!='BOTH_FILLED'];csv_save('missing_codes.csv',missing,columns)
    csv_save('code_conflicts.csv',[r for r in rows if r['review']],columns)
    save('examples.json',missing[:10])
    groups=[]
    for account,_,_ in credentials:
        for archived in (False,True):
            subset=[r for r in rows if r['account']==account and r['archived']==archived]
            groups.append({'account':account,'archived':archived,'total':len(subset),
                'both_filled':sum(r['status']=='BOTH_FILLED' for r in subset),
                'missing_okpd2':sum(not r['okpd2'] for r in subset),'missing_tnved':sum(not r['tnved'] for r in subset),
                'both_empty':sum(r['status']=='BOTH_EMPTY' for r in subset),'review':sum(bool(r['review']) for r in subset)})
    known=query('SELECT account,offer_id FROM raw_yandex_offer')
    observed={(r['account'],r['offerId']) for r in rows}
    not_seen=[{'account':r['account'],'offerId':r['offer_id']} for r in known if (r['account'],r['offer_id']) not in observed]
    csv_save('saved_ids_not_observed.csv',not_seen,['account','offerId'])
    summary={'read_started_at':start,'read_finished_at':now(),'complete':all(s['exhausted'] for s in scans),
             'campaigns':len(campaigns),'businesses':len({r['businessId'] for r in rows}),'total':len(rows),'groups':groups,
             'scans':scans,'saved_ids_not_observed':len(not_seen),'external_writes':0,'db_writes':0,
             'source':'POST offer-mappings read; commodityCodes plus legacy customsCommodityCode'}
    save('summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
