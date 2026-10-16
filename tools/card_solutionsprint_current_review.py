#!/usr/bin/env python3
# поток: card
"""Read-only current Solutions Print names and warehouse stock audit."""
import json
import csv
import re
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from core.db import query
from prices import loader, blacklist
from prices.profiles import get_identity
from tools.card_solutionsprint_description_audit import (
    IDS, fetch, listing, max_page, normalized, supplier_code, cls, text)
from tools.card_solutionsprint_scraper import login, parse_card

OUT = ROOT / 'docs/reports/card_solutionsprint_current_review_2026-10-12'
IDS = {**IDS, 'c16b41ef-3428-11ed-0a80-0e48002629ce': 'Спринт (ИП Яхин)',
       '59ae3427-7e0e-11ee-0a80-04960008d1f8': 'Спринт (ИП Зельман)'}

def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')

def parse_stocks(raw):
    stocks={}
    for m in re.finditer(r'Склад\s+([^:—–-]+?)\s*[-:—–]\s*([\d\s]+(?:[.,]\d+)?)\s*шт\.?',raw,re.I):
        key=m[1].strip().casefold()
        qty=float(re.sub(r'\s+','',m[2]).replace(',','.'))
        if qty.is_integer():qty=int(qty)
        if key in stocks and stocks[key]!=qty:raise RuntimeError('Conflicting warehouse quantity')
        stocks[key]=qty
    return stocks

def extract(doc):
    result = []
    for block in doc.xpath('//*[@data-entity="item"]'):
        wrapper = html.Element('div')
        # Copy only this item, never collect recommendation text from other cards.
        wrapper.append(html.fromstring(html.tostring(block)))
        rows = listing(wrapper)
        if len(rows) != 1:
            continue
        raw = text(block)
        stocks = parse_stocks(raw)
        result.append({**rows[0], 'warehouse_stocks': stocks,
                       'availability_excerpt': raw[:raw.find('Быстрый просмотр')] if 'Быстрый просмотр' in raw else raw[:120],
                       'under_order': bool(re.search(r'под\s+заказ',raw,re.I))})
    if len(result) != len(listing(doc)):
        raise RuntimeError('Listing extraction count mismatch')
    return result

def collect_ms():
    def supplier(sid):
        return list(ms_api.iter_rows('/entity/product', {'filter':f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'},page=100))
    with ThreadPoolExecutor(max_workers=2) as pool:
        cards = [c for group in pool.map(supplier, IDS) for c in group]
    assert len({c['id'] for c in cards}) == len(cards)
    save('ms_active_snapshot.json',cards)
    docs=[]
    for key in ('s_print_msk','s_print_spb'):
        found=loader.existing_docs(get_identity(key))
        if found:
            prefix=max(d['name'].rsplit('_p',1)[0] for d in found)
            docs.extend(d for d in found if d['name'].startswith(prefix+'_p'))
    save('current_docs.json',docs)
    def positions(d):
        r=ms_api.get('/entity/enter/'+d['id']+'/positions',{'limit':1000})
        assert r['meta']['size'] <= 1000
        return {'id':d['id'],'name':d['name'],'positions':r['rows']}
    with ThreadPoolExecutor(max_workers=3) as pool:
        save('current_positions.json',list(pool.map(positions,docs)))
    print('MS active='+str(len(cards))+' current_docs='+str(len(docs)),flush=True)

def collect_catalog():
    session=requests.Session();login(session)
    root=fetch(session,'/catalog/')
    cats=list(dict.fromkeys(a.get('href') for a in root.xpath('//li['+cls('selection-device-item')+']/a[@href]') if a.get('href').startswith('/catalog/')))
    assert '/catalog/kartridzhi/' in cats
    local=threading.local();lock=threading.Lock();next_start=[0.0]
    def doc(path,params=None):
        if not hasattr(local,'session'):
            local.session=requests.Session();login(local.session)
        with lock:
            time.sleep(max(0,next_start[0]-time.monotonic()));next_start[0]=time.monotonic()+.4
        return fetch(local.session,path,params)
    all_rows=[];done=0
    with ThreadPoolExecutor(max_workers=4) as pool:
        totals={}
        for task,path in [(pool.submit(doc,path),path) for path in cats]:
            d=task.result();totals[path]=max_page(d)
            rows=extract(d);all_rows.extend(rows)
            save('source_pages/'+path.strip('/').replace('/','_')+'_0001.json',rows)
        tasks={pool.submit(doc,path,{'PAGEN_1':n}):(path,n) for path,total in totals.items() for n in range(2,total+1)}
        for task in as_completed(tasks):
            path,n=tasks[task];rows=extract(task.result())
            if not rows: raise RuntimeError('Empty paginated listing')
            save('source_pages/'+path.strip('/').replace('/','_')+f'_{n:04d}.json',rows)
            all_rows.extend(rows);done+=1
            if done%30==0 or done==len(tasks):
                save('progress.json',{'pages':done+len(cats),'total_pages':sum(totals.values()),'at':datetime.now(timezone.utc).isoformat()})
                print(f'catalogue pages {done+len(cats)}/{sum(totals.values())}',flush=True)
        # Search only unresolved current receipt cards, not historical assortment.
        cards=json.loads((OUT/'ms_active_snapshot.json').read_text())
        positions=json.loads((OUT/'current_positions.json').read_text())
        entered={ms_api.meta_id(p,'assortment') for d in positions for p in d['positions']}
        seen={r['supplier_code'] for r in all_rows}
        missing={supplier_code(c) for c in cards if c['id'] in entered and supplier_code(c) and supplier_code(c) not in seen}
        for task,code in [(pool.submit(doc,'/search/index.php',{'q':code,'s':'Поиск'}),code) for code in sorted(missing)]:
            rows=[r for r in extract(task.result()) if r['supplier_code']==code]
            all_rows.extend(rows);save('source_pages/search_'+code+'.json',rows)
    # Category repeats must agree; ambiguity retained for review if stock moved mid-scan.
    unique={json.dumps(r,ensure_ascii=False,sort_keys=True):r for r in all_rows}
    save('source_listings.json',list(unique.values()))
    save('catalog_metadata.json',{'at_utc':datetime.now(timezone.utc).isoformat(),'pages':sum(totals.values()),'searched_current_codes':len(missing)})
    samples=[]
    for row in sorted(unique.values(),key=lambda r:r['supplier_code']):
        if not row['warehouse_stocks'] or not any(row['warehouse_stocks'].values()):continue
        if any(r['code']==row['supplier_code'] for r in samples):continue
        detail=parse_card(html.tostring(fetch(session,row['url'])),row['url'],authenticated=True)
        samples.append({'code':row['supplier_code'],'name_equal':normalized(row['name'])==normalized(detail['name']),
                        'listing_stocks':row['warehouse_stocks'],'detail_stocks':detail['warehouse_stocks']})
        assert detail['supplier_code']==row['supplier_code'] and samples[-1]['name_equal']
        if len(samples)==6:break
    save('listing_detail_verification.json',samples)
    assert len(samples)==6

def analyse():
    cards=json.loads((OUT/'ms_active_snapshot.json').read_text());source=json.loads((OUT/'source_listings.json').read_text())
    positions=json.loads((OUT/'current_positions.json').read_text())
    entered={ms_api.meta_id(p,'assortment') for d in positions for p in d['positions']}
    # Third supplier discovered from live receipt IDs. Only positive source stock is actionable.
    for name in ('additional_supplier_active_snapshot.json','fourth_supplier_active_snapshot.json'):
        extra=OUT/name
        if extra.exists():
            known={c['id'] for c in cards}
            cards.extend(c for c in json.loads(extra.read_text()) if c['id'] not in known)
    save('scoped_ms_cards.json',cards)
    black={r['article_norm'] for r in query('SELECT article_norm FROM prc_blacklist WHERE article_norm=ANY(%s)',([str(c.get('article')or'').strip().upper() for c in cards],))}
    save('blacklist_snapshot.json',sorted(black))
    index=defaultdict(list)
    for o in source:
        o['warehouse_stocks']=parse_stocks(o['availability_excerpt'])
        index[o['supplier_code']].append(o)
    review=[]
    for c in cards:
        code=supplier_code(c);group=IDS[ms_api.meta_id(c,'supplier')];warehouse='мск' if group=='Спринт МСК' else 'спб'
        found=index.get(code,[])
        r={'id':c['id'],'external_code':c.get('externalCode'),'article':c.get('article'),'group':group,'warehouse':warehouse,
           'entered':c['id'] in entered,'ms_name':c['name'],'old_description':c.get('description') or '',
           'source_name':'','source_url':'','stock':None,'status':'','supplier_code':code}
        if not code:r['status']='INVALID_ARTICLE'
        elif not found:r['status']='NO_CURRENT_SOURCE_MATCH'
        elif len(found)!=1:r['status']='AMBIGUOUS_CURRENT_SOURCE'
        else:
            o=found[0];r.update(source_name=o['name'],source_url=o['url'],stock=o['warehouse_stocks'].get(warehouse))
            if r['stock'] is None:
                r['status']='SOURCE_UNDER_ORDER' if o['under_order'] else 'SOURCE_NOT_AVAILABLE' if re.search(r'нет\s+в\s+наличии',o['availability_excerpt'],re.I) else 'WAREHOUSE_STOCK_UNKNOWN'
            elif r['stock']<=0:r['status']='ZERO_RELEVANT_WAREHOUSE_STOCK'
            elif 'брак' in c['name'].casefold():r['status']='EXCLUDED_DEFECTIVE'
            elif str(c.get('article')or'').strip().upper() in black:r['status']='EXCLUDED_BLACKLIST'
            elif blacklist.in_rules(c.get('article'),'s_print_msk' if warehouse=='мск' else 's_print_spb'):r['status']='EXCLUDED_SUPPLIER_SET'
            elif blacklist.in_damaged(o['name']) or blacklist.in_kinds(o['name']):r['status']='EXCLUDED_KIND_OR_DAMAGED'
            elif re.search(r'^\s*(?:фото)?бумаг|^\s*пл[её]нк|^\s*термотрансфер',o['name'],re.I):r['status']='EXCLUDED_KIND'
            elif normalized(r['old_description'])==normalized(o['name']):r['status']='ALREADY_EQUAL'
            else:r['status']='DESCRIPTION_DIFFERENCE'
        review.append(r)
    save('review.json',review)
    for name,rows in [('all_cards.csv',review),('description_differences.csv',[r for r in review if r['status']=='DESCRIPTION_DIFFERENCE'])]:
        if rows:
            with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)
    unresolved=[r for r in review if r['entered'] and r['status'] in {'INVALID_ARTICLE','NO_CURRENT_SOURCE_MATCH','AMBIGUOUS_CURRENT_SOURCE','WAREHOUSE_STOCK_UNKNOWN'}]
    save('unresolved_current_receipt_cards.json',unresolved)
    summary={'at_utc':datetime.now(timezone.utc).isoformat(),'active_ms_cards':len(cards),'source_codes':len(index),
             'statuses':dict(Counter(r['status'] for r in review)),
             'positive_cards':sum((r['stock']or 0)>0 for r in review),
             'current_receipt_documents':len(positions),'current_receipt_positions':sum(len(d['positions']) for d in positions),
             'receipt_statuses':dict(Counter(r['status'] for r in review if r['entered'])),
             'receipt_ids_outside_scoped_cards':len(entered-{c['id'] for c in cards}),
             'unresolved_current_receipt_cards':len(unresolved),'external_writes':0,'database_writes':0}
    save('summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':
    OUT.mkdir(exist_ok=True);(OUT/'source_pages').mkdir(exist_ok=True)
    if '--saved' not in sys.argv:
        collect_ms();collect_catalog()
    analyse()
