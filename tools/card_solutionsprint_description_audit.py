#!/usr/bin/env python3
# поток: card
"""Read-only complete active MS description audit against authenticated supplier listings."""
import argparse
import csv
import json
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from dotenv import load_dotenv
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP
from tools.card_solutionsprint_scraper import login, parse_card, text, cls

OUT = ROOT / 'docs/reports/card_solutionsprint_description_audit_2026-10-11'
BASE = 'https://cartridge.ru'
IDS = {sid:group for sid,group in ID2GROUP.items() if group in ('Спринт','Спринт МСК')}


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def supplier_code(card):
    group = IDS.get(ms_api.meta_id(card, 'supplier'))
    pattern = r'SP(\d+)_MSK' if group == 'Спринт МСК' else r'SP(\d+)'
    m = re.fullmatch(pattern, (card.get('article') or '').strip(), re.I)
    return m.group(1) if m else ''


def fetch(session, path, params=None):
    for attempt in range(3):
        try:
            r = session.get(urljoin(BASE, path), params=params, timeout=(15,45))
            if r.status_code == 200 and urlparse(r.url).hostname == 'cartridge.ru':
                return html.fromstring(r.content)
        except requests.RequestException:
            pass
        time.sleep(2*(attempt+1))
    raise RuntimeError('Supplier GET failed: '+path)


def listing(doc):
    rows=[]
    for block in doc.xpath('//*[@data-entity="item"]'):
        codes=block.xpath('.//*['+cls('product-cart__item-code')+']')
        names=block.xpath('.//*['+cls('product-cart__title')+']//a[@href]')
        if len(codes)!=1 or len(names)!=1:
            continue
        m=re.fullmatch(r'Код товара\s*:\s*(\d+)',text(codes[0]))
        path=names[0].get('href')
        if m and text(names[0]) and re.fullmatch(r'/catalog/[^/]+/',path):
            rows.append({'supplier_code':m.group(1),'name':text(names[0]),'url':urljoin(BASE,path)})
    return rows


def max_page(doc):
    values=[1]
    for a in doc.xpath('//a[contains(@href,"PAGEN_1")]'):
        v=parse_qs(urlparse(a.get('href')).query).get('PAGEN_1',['1'])[0]
        if v.isdigit():values.append(int(v))
    return max(values)


def csv_save(name, rows):
    if rows:
        with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--saved',action='store_true')
    parser.add_argument('--resume-pages',action='store_true');args=parser.parse_args()
    OUT.mkdir(exist_ok=True);load_dotenv(ROOT/'.env')
    cards=json.loads((OUT/'ms_before.json').read_text())
    wanted={supplier_code(c) for c in cards if supplier_code(c)}
    index=defaultdict(dict)
    pages=[]
    if args.saved:
        for row in json.loads((OUT/'source_titles.json').read_text()):index[row['supplier_code']][row['url']]=row
        pages=json.loads((OUT/'scanned_pages.json').read_text())
    else:
        session=requests.Session();login(session)
        if args.resume_pages:
            progress=json.loads((OUT/'progress.json').read_text())
            files=sorted((OUT/'source_pages').glob('catalog_*_*.json'))
            assert progress['pages']==progress['total_pages']==len(files)
            categories=[]
            for file in files:
                section,page=file.stem.rsplit('_',1)
                path='/catalog/'+section.removeprefix('catalog_')+'/'
                rows=json.loads(file.read_text())
                pages.append({'path':path,'page':int(page),'items':len(rows)})
                for row in rows:
                    if row['supplier_code'] in wanted:index[row['supplier_code']][row['url']]=row
        else:
            root=fetch(session,'/catalog/')
            categories=list(dict.fromkeys(a.get('href') for a in root.xpath('//li['+cls('selection-device-item')+']/a[@href]')
                                          if a.get('href').startswith('/catalog/')))
            assert categories and '/catalog/kartridzhi/' in categories
        categories.sort(key=lambda x:x!='/catalog/kartridzhi/')
        (OUT/'source_pages').mkdir(exist_ok=True)
        local=threading.local()
        rate_lock=threading.Lock()
        next_start=[0.0]
        def worker_session():
            if not hasattr(local,'session'):
                local.session=requests.Session();login(local.session)
            return local.session
        def supplier_doc(path,params=None):
            with rate_lock:
                time.sleep(max(0,next_start[0]-time.monotonic()))
                next_start[0]=time.monotonic()+.35
            return fetch(worker_session(),path,params)
        def page_rows(path,n):
            file=OUT/'source_pages'/ (path.strip('/').replace('/','_')+f'_{n:04d}.json')
            if file.exists():
                try:return json.loads(file.read_text())
                except json.JSONDecodeError:pass
            doc=supplier_doc(path,{'PAGEN_1':n} if n>1 else None)
            rows=listing(doc)
            save('source_pages/'+file.name,rows)
            return rows
        # Independent Bitrix sessions avoid one-session server serialization.
        # Maximum four in flight, with <=3 request starts/sec across workers.
        with ThreadPoolExecutor(max_workers=4) as pool:
            first_tasks={pool.submit(supplier_doc,path):path for path in categories}
            totals={}
            for task in as_completed(first_tasks):
                path=first_tasks[task];doc=task.result();total=max_page(doc)
                totals[path]=total
                rows=listing(doc)
                save('source_pages/'+path.strip('/').replace('/','_')+'_0001.json',rows)
            futures={pool.submit(page_rows,path,n):(path,n,totals[path])
                     for path in categories for n in range(1,totals[path]+1)}
            for task in as_completed(futures):
                path,n,total=futures[task];rows=task.result()
                if not rows and total>1:raise RuntimeError('Empty paginated listing; stop')
                pages.append({'path':path,'page':n,'total_pages':total,'items':len(rows)})
                for row in rows:
                    if row['supplier_code'] in wanted:index[row['supplier_code']][row['url']]=row
                if len(pages)%20==0 or len(pages)==len(futures):
                    save('progress.json',{'phase':'catalogue','pages':len(pages),'matched_codes':len(index),
                                          'wanted_codes':len(wanted),'total_pages':len(futures),
                                          'at':datetime.now(timezone.utc).isoformat()})
                    print(f'pages={len(pages)}/{len(futures)} matched={len(index)}/{len(wanted)}',flush=True)
        # Codes not exposed by category navigation: search exact code, never fuzzy name.
        missing=sorted(wanted-set(index))
        def search_code(code):
            file=OUT/'source_pages'/('search_'+code+'.json')
            if file.exists():return json.loads(file.read_text())
            rows=listing(supplier_doc('/search/index.php',{'q':code,'s':'Поиск'}))
            save('source_pages/'+file.name,rows)
            return rows
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks={pool.submit(search_code,code):code for code in missing}
            for n,task in enumerate(as_completed(tasks),1):
                code=tasks[task]
                for row in task.result():
                    if row['supplier_code']==code:index[code][row['url']]=row
                if n%25==0 or n==len(tasks):
                    save('search_progress.json',{'checked':n,'total':len(tasks),'matched_codes':len(index),
                                                'at':datetime.now(timezone.utc).isoformat()})
                    print(f'search={n}/{len(tasks)} matched={len(index)}/{len(wanted)}',flush=True)
        source=[row for values in index.values() for row in values.values()]
        save('source_titles.json',source);save('scanned_pages.json',pages)
        # Verify that listing names are complete by checking both equal/different samples.
        samples=[]
        seen=set()
        for differs in (True,False):
            for card in cards:
                code=supplier_code(card);found=list(index.get(code,{}).values())
                if len(found)!=1 or code in seen:continue
                row=found[0]
                if (normalized(card.get('description'))!=normalized(row['name']))!=differs:continue
                try:
                    detail=parse_card(html.tostring(fetch(session,urlparse(row['url']).path)),
                                      row['url'],authenticated=True)
                except (RuntimeError,ValueError):
                    continue
                assert detail['supplier_code']==code
                samples.append({'code':code,'url':row['url'],'listing_name':row['name'],
                                'detail_name':detail['name'],'equal':row['name']==detail['name']})
                seen.add(code)
                if len(samples)>=(6 if differs else 12):break
                time.sleep(.3)
        save('listing_detail_verification.json',samples)
        if len(samples)<8 or not all(r['equal'] for r in samples):
            raise RuntimeError('Full listing names not confirmed; review detail samples')
    report=[]
    for c in cards:
        code=supplier_code(c);found=list(index.get(code,{}).values());r={'id':c['id'],'external_code':c.get('externalCode') or '',
            'article':c.get('article') or '', 'supplier_group':IDS[ms_api.meta_id(c,'supplier')],
            'supplier_code':code,'ms_name':c.get('name') or '','old_description':c.get('description') or '',
            'source_name':'','source_url':'','status':''}
        if not code:r['status']='INVALID_ARTICLE'
        elif not found:r['status']='NO_SITE_CODE_MATCH'
        elif len(found)!=1:r['status']='AMBIGUOUS_SITE_CODE'
        else:
            r.update(source_name=found[0]['name'],source_url=found[0]['url'])
            r['status']='EXACT' if r['old_description']==r['source_name'] else 'WHITESPACE_ONLY' if normalized(r['old_description'])==normalized(r['source_name']) else 'CONTENT_DIFFERENCE'
        report.append(r)
    changed=[r for r in report if r['status']=='CONTENT_DIFFERENCE']
    save('review.json',report);csv_save('all_cards.csv',report);csv_save('description_differences.csv',changed)
    csv_save('unmatched.csv',[r for r in report if r['status'] in ('INVALID_ARTICLE','NO_SITE_CODE_MATCH','AMBIGUOUS_SITE_CODE')])
    summary={'at_utc':datetime.now(timezone.utc).isoformat(),'active_ms_cards':len(cards),'unique_valid_supplier_codes':len(wanted),
             'card_statuses':dict(Counter(r['status'] for r in report)),'by_group':{g:dict(Counter(r['status'] for r in report if r['supplier_group']==g)) for g in sorted(set(IDS.values()))},
             'source_pages':len(pages),'description_content_changes':len(changed),'supplier_stock_filter':False,
             'external_writes':0,'database_writes':0,'status':'complete_read_only'}
    save('summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
