#!/usr/bin/env python3
# поток: card
"""Read-only wave-2 chip audit: source names first, positive supplier stock only."""
import argparse
import ast
from collections import Counter,defaultdict
from datetime import datetime,timezone
import csv
import hashlib
import json
from pathlib import Path
import re
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.db import query
from prices.mailbox import fetch_latest_price
from prices.parser import parse
from prices.profiles import get_profile
from prices.supplier_group import own_ids
from invoice_bot.supplier_groups import ID2GROUP,ID2NAME
from collectors.moysklad import _get,API
from ops.wb_supplier_chip_evidence import recognize

OUT=ROOT/'docs/reports/card_chip_wave2_recheck_2026-10-07'
SOURCE=ROOT/'docs/reports/tc_chip_wave2_left_2026-09-24.csv'
LABELS={'с чипом':'chip','чип без счётчика':'chip_free','без чипа':'nochip'}

def now():return datetime.now(timezone.utc).isoformat()
def save(name,data):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,default=str,indent=2)+'\n')
def csv_save(name,rows,cols):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=cols,delimiter=';');w.writeheader();w.writerows(rows)
def norm(text):return re.sub(r'\s+',' ',str(text or '').replace('\xa0',' ')).strip().casefold().replace('ё','е')
def article_key(text):return re.sub(r'\s+','',str(text or '')).upper()
def classes(text,supplier):
    data=recognize(text,supplier)
    return {LABELS[c] for c in data['classes']},data['evidence']

def collect():
    OUT.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader(SOURCE.open(encoding='utf-8-sig'),delimiter=';'))
    codes=sorted({r['внешний код'] for r in rows});assert len(codes)==691
    save('scope.json',{'at':now(),'source':str(SOURCE.relative_to(ROOT)),
         'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'codes':codes,'scope':691,
         'instruction':'Пока ничего не меняем, только проверяем','supplier_stock_filter':'Удаленный склад, stock > 0',
         'order':['supplier source versus current MS names','stock-positive chip evidence','supplier dictionary','TC set propagation']})
    stock=query("""SELECT s.captured_at,s.ms_id,s.external_code,s.supplier,s.stock,s.name AS stock_name,
        p.name AS ms_name,p.article,p.code AS ms_code,p.archived,p.updated_at,
        r.payload->'supplier'->'meta'->>'href' AS supplier_href
        FROM supplier_stock s LEFT JOIN ms_product p ON p.ms_id=s.ms_id
        LEFT JOIN raw_moysklad_product r ON r.ms_id=s.ms_id
        WHERE s.captured_at=(SELECT max(captured_at) FROM supplier_stock)
        AND s.store='Удаленный склад' AND s.stock>0 AND s.external_code=ANY(%s)
        ORDER BY s.external_code,s.supplier,s.ms_id""",(codes,))
    # The raw-table identifier may differ across deployments; SELECT is kept narrow.
    for r in stock:
        sid=str(r.get('supplier_href') or '').rstrip('/').split('/')[-1]
        r['supplier_id']=sid;r['supplier_group']=ID2GROUP.get(sid) or r.get('supplier') or 'UNKNOWN'
    save('positive_stock_cards.json',stock)
    models=query("SELECT external_code,title,consumable_type,chip,additional_title,last_seen,gone_at FROM prc_tc_model ORDER BY external_code")
    save('tc_models.json',models)
    sets=query('SELECT external_code,components,resolved_at,updated_at FROM set_cost')
    for r in sets:
        clean=[]
        for value in r['components'] or []:
            if str(value).startswith('{'):value=ast.literal_eval(str(value)).get('external_code')
            if value:clean.append(str(value))
        r['components']=clean
    save('tc_sets_cache.json',sets)
    latest=query("""WITH l AS (SELECT DISTINCT ON(supplier_key) id,supplier_key,moment,source_file
        FROM prc_price_load WHERE source_kind='mail' AND status='ok' AND NOT dry_run
        ORDER BY supplier_key,moment DESC)
        SELECT l.supplier_key,l.moment,l.source_file,r.article,r.name,r.qty,r.ms_id,r.row_no
        FROM l JOIN prc_price_row r ON r.load_id=l.id WHERE r.ms_id=ANY(%s)""",([r['ms_id'] for r in stock],))
    save('latest_linked_price_rows.json',latest)
    print('scope',len(codes),'positive supplier cards',len(stock),'positive codes',len({r['external_code'] for r in stock}),flush=True)

def source_names():
    stock=json.loads((OUT/'positive_stock_cards.json').read_text())
    linked=json.loads((OUT/'latest_linked_price_rows.json').read_text())
    selected=[];inventory=[]
    for key in ('colortek','kaktus_msk','odissey','sakura'):
        profile=get_profile(key)
        letter=fetch_latest_price(profile.mail_folder,pattern=profile.file_pattern,profile=profile)
        if not letter:inventory.append({'supplier_key':key,'status':'NO_PRICE'});continue
        extension=Path(letter['filename']).suffix.lower();file=OUT/f'raw/{key}/price{extension}'
        file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(letter['content'])
        parsed,header=parse(letter['content'],profile)
        save(f'raw/{key}/parsed.json',parsed)
        inventory.append({'supplier_key':key,'filename':letter['filename'],'letter_date':letter['date'],
            'sha256':hashlib.sha256(letter['content']).hexdigest(),'rows':len(parsed),'header_row':header,'read_at':now(),'status':'READ'})
        current=defaultdict(list)
        for r in parsed:
            if r.get('qty') is not None and float(r['qty'])>0:current[article_key(r['article'])].append(r)
        relations=defaultdict(list)
        for r in linked:
            if r['supplier_key']==key:relations[r['ms_id']].append(r)
        ids=own_ids(profile)
        for card in stock:
            if card['supplier_id'] not in ids:continue
            original_articles={article_key(r['article']) for r in relations.get(card['ms_id'],[])}
            original_articles.add(article_key(card['article']))
            matches={r['row']:r for article in original_articles for r in current.get(article,[])}
            if len(matches)!=1:continue
            source=next(iter(matches.values()))
            selected.append({**card,'source_key':key,'supplier_name':source['name'],'supplier_qty':source['qty'],
                'source_article':source['article'],'source_row':source['row'],'source_file':letter['filename'],
                'source_letter_date':letter['date'],'mapping':'exact article + exact supplier group + journal ms_id when available'})
        print('source',key,'rows',len(parsed),'matched positive cards',sum(r['source_key']==key for r in selected),flush=True)
        save('source_inventory.json',inventory);save('fresh_source_linked_cards.json',selected)
    # Stratify: chip-free / explicit ordinary / no-chip / silent, up to 4 per supplier.
    samples=[]
    for key in ('colortek','kaktus_msk','odissey','sakura'):
        items=[r for r in selected if r['source_key']==key]
        chosen=[]
        for typ in ('chip_free','chip','nochip','silent'):
            candidates=[r for r in items if (typ in classes(r['supplier_name'],r['supplier_group'])[0] if typ!='silent' else not classes(r['supplier_name'],r['supplier_group'])[0])]
            if candidates:
                row=sorted(candidates,key=lambda r:(r['external_code'],r['ms_id']))[0]
                if row['ms_id'] not in {r['ms_id'] for r in chosen}:chosen.append(row)
        for row in sorted(items,key=lambda r:(norm(r['supplier_name'])==norm(r['ms_name']),r['external_code'],r['ms_id'])):
            if len(chosen)>=4:break
            if row['ms_id'] not in {r['ms_id'] for r in chosen}:chosen.append(row)
        for row in chosen:
            live=_get(f'{API}/entity/product/{row["ms_id"]}')
            save(f'raw/ms_samples/{row["ms_id"]}.json',live)
            live_sid=str(live.get('supplier',{}).get('meta',{}).get('href','')).rstrip('/').split('/')[-1]
            if live.get('id')!=row['ms_id'] or live.get('externalCode')!=row['external_code'] or live_sid!=row['supplier_id']:
                raise ValueError('Live MS sample identity changed')
            a,ae=classes(row['supplier_name'],row['supplier_group']);b,be=classes(live['name'],row['supplier_group'])
            samples.append({'supplier_key':key,'external_code':row['external_code'],'ms_id':row['ms_id'],
                'article':row['source_article'],'supplier_qty':row['supplier_qty'],'ms_remote_stock':row['stock'],
                'supplier_name':row['supplier_name'],'ms_current_name':live['name'],
                'name_status':'EXACT' if row['supplier_name']==live['name'] else 'NORMALIZED_EQUAL' if norm(row['supplier_name'])==norm(live['name']) else 'DIFFERENT',
                'supplier_chip_classes':';'.join(sorted(a)),'ms_chip_classes':';'.join(sorted(b)),
                'chip_class_equal':a==b,'supplier_mentions':'; '.join(ae),'ms_mentions':'; '.join(be),
                'source_file':row['source_file'],'source_date':row['source_letter_date'],'read_at':now()})
    save('sample_checks.json',samples)
    if samples:csv_save('01_supplier_vs_ms_samples.csv',samples,list(samples[0]))
    print('samples',len(samples),'name statuses',dict(Counter(r['name_status'] for r in samples)),
          'chip class mismatches',sum(not r['chip_class_equal'] for r in samples),flush=True)

def evidence():
    stock=json.loads((OUT/'positive_stock_cards.json').read_text())
    source={r['ms_id']:r for r in json.loads((OUT/'fresh_source_linked_cards.json').read_text())}
    website={r['ms_id']:r for r in json.loads((OUT/'01_bulat_site_samples.json').read_text()) if r['exact_article_verified']}
    linked=json.loads((OUT/'latest_linked_price_rows.json').read_text())
    models={r['external_code']:r for r in json.loads((OUT/'tc_models.json').read_text())}
    codes=json.loads((OUT/'scope.json').read_text())['codes']
    source_groups={sid:key for key in ('colortek','kaktus_msk','odissey','sakura') for sid in own_ids(get_profile(key))}
    indices={}
    for key in set(source_groups.values()):
        idx=defaultdict(list)
        for r in json.loads((OUT/f'raw/{key}/parsed.json').read_text()):idx[article_key(r['article'])].append(r)
        indices[key]=idx
    journal_links=defaultdict(list)
    for r in linked:journal_links[(r['supplier_key'],r['ms_id'])].append(r['article'])
    observed=[];by_code=defaultdict(list);patterns=defaultdict(list)
    for r in stock:
        src=source.get(r['ms_id']);key=source_groups.get(r['supplier_id'])
        excluded=None
        if not src and key:
            arts={article_key(r['article'])}|{article_key(a) for a in journal_links[(key,r['ms_id'])]}
            raw_rows={v['row']:v for a in arts for v in indices[key].get(a,[])}
            if raw_rows and all(x.get('qty') is not None and float(x['qty'])<=0 for x in raw_rows.values()):
                excluded='EXCLUDED_SOURCE_NO_STOCK'
            elif raw_rows and all(x.get('qty') is None for x in raw_rows.values()):excluded='EXCLUDED_UNKNOWN_SOURCE_STOCK'
        web=website.get(r['ms_id'])
        text=src['supplier_name'] if src else web['supplier_name'] if web else r['ms_name']
        cls,mentions=classes(text,r['supplier_group']) if not excluded else (set(),[])
        # Generic unlimited marketing language without a chip mention is insufficient.
        if 'chip_free' in cls and not re.search(r'чип|chip',norm(text)):cls.remove('chip_free')
        level='FRESH_SUPPLIER_PRICE' if src else 'FRESH_SUPPLIER_SITE_MS_STOCK' if web else 'MS_NAME_ONLY_UNVERIFIED_SOURCE'
        row={'external_code':r['external_code'],'ms_id':r['ms_id'],'supplier_group':r['supplier_group'],
             'supplier_name':r['supplier'],'article':src['source_article'] if src else r['article'],
             'remote_stock':r['stock'],'source_qty':src['supplier_qty'] if src else '',
             'evidence_level':level,'text_used':text if not excluded else '',
             'classes':';'.join(sorted(cls)),'mentions':'; '.join(mentions),'status':excluded or 'CHECKED',
             'source_file':src['source_file'] if src else web['url'] if web else 'MS snapshot',
             'source_date':src['source_letter_date'] if src else web['read_at'] if web else r['captured_at']}
        observed.append(row);by_code[r['external_code']].append(row)
        if not excluded:
            for typ in cls:
                for mention in [m for m in mentions if m!='явная положительная формулировка'] or ['REVIEW_LITERAL_PATTERN']:
                    patterns[(r['supplier_group'],typ,mention,level)].append(row)
    csv_save('02_in_stock_supplier_evidence.csv',observed,list(observed[0]))
    result=[]
    for code in codes:
        rows=by_code[code];confirmed=set();provisional=set()
        for r in rows:
            values=set(filter(None,r['classes'].split(';')))
            (confirmed if r['evidence_level'].startswith('FRESH_SUPPLIER_') else provisional).update(values)
        positive=confirmed&{'chip','chip_free'};unknown_positive=provisional&{'chip','chip_free'}
        model=models.get(code,{})
        proposed=next(iter(positive)) if len(positive)==1 else ''
        if not rows:status='NO_POSITIVE_SUPPLIER_STOCK'
        elif len(positive)>1:status='SOURCE_CHIP_TYPE_CONFLICT'
        elif proposed:status='SOURCE_CONFIRMED_POSITIVE'
        elif unknown_positive:status='MS_POSITIVE_UNVERIFIED_SOURCE'
        elif confirmed=={'nochip'}:status='SOURCE_ONLY_NOCHIP'
        else:status='NO_EXPLICIT_POSITIVE'
        result.append({'external_code':code,'tc_title':model.get('title',''),'tc_chip':model.get('chip') or '',
            'tc_active':bool(model and not model.get('gone_at')),'positive_supplier_cards':len(rows),
            'confirmed_classes':';'.join(sorted(confirmed)),'ms_only_classes':';'.join(sorted(provisional)),
            'has_supplier_says_chip':bool(positive),'proposed_class':proposed,
            'proposed_value':{'chip':'с чипом','chip_free':'с чипом без счётчика'}.get(proposed,''),
            'other_supplier_says_nochip':'nochip' in confirmed|provisional,
            'already_matches_tc':bool(proposed and model.get('chip')==proposed),'status':status})
    csv_save('02_wave2_results.csv',result,list(result[0]));save('wave2_results.json',result)
    bank=[]
    for (group,typ,mention,level),items in sorted(patterns.items()):
        bank.append({'supplier_group':group,'class':typ,'value':{'chip':'с чипом','chip_free':'с чипом без счётчика','nochip':'без чипа'}[typ],
             'inclusion':mention,'evidence_level':level,'in_stock_cards':len({r['ms_id'] for r in items}),
             'example_code':items[0]['external_code'],'example_article':items[0]['article'],'example_name':items[0]['text_used']})
    if bank:csv_save('03_supplier_chip_inclusions.csv',bank,list(bank[0]))
    rules=[
        {'scope':'all','class':'chip','rule':'с чипом / with chip / New chip / отдельное слово chip или чип в названии комплектного расходника','priority':3},
        {'scope':'all','class':'chip_free','rule':'чип без счётчика / чип безлимит / chip unlimited / chip no count; требуется упоминание чипа','priority':1},
        {'scope':'all','class':'nochip','rule':'без чипа / б/ч / б.ч / w/o chip / no chip / chipless / чип отдельно / требуется чип','priority':1},
        {'scope':'Блоссом only','class':'nochip','rule':'W/chip, сохранённое подтверждённое владельцем исключение docs/MS_CARD_FIELDS.md','priority':0},
        {'scope':'all','class':'unknown','rule':'не требуется чип — не самостоятельное доказательство; молчание не означает отсутствие','priority':0},
        {'scope':'all','class':'unknown','rule':'Longlife / многоразовый сами по себе не доказывают отсутствие счётчика','priority':0},
        {'scope':'Кактус','class':'unknown','rule':'default_chip=chip не используется в задаче явного упоминания в названии','priority':0}]
    save('03_rules.json',{'source_files':['prices/features.py','ops/wb_supplier_chip_evidence.py','docs/MS_CARD_FIELDS.md'],
                         'rules':rules,'production_dictionary_changed':False})
    sample_rows=json.loads((OUT/'sample_checks.json').read_text())
    keys_by_group={ID2GROUP.get(sid):key for key in ('colortek','kaktus_msk','odissey','sakura') for sid in own_ids(get_profile(key))}
    coverage=[]
    for group in sorted({r['supplier_group'] for r in stock}):
        group_rows=[r for r in observed if r['supplier_group']==group]
        key=keys_by_group.get(group)
        coverage.append({'supplier_group':group,'positive_stock_cards':len(group_rows),
             'fresh_price_name_cards':sum(r['evidence_level']=='FRESH_SUPPLIER_PRICE' for r in group_rows),
             'fresh_site_name_cards':sum(r['evidence_level']=='FRESH_SUPPLIER_SITE_MS_STOCK' for r in group_rows),
             'sampled_live_ms_cards':sum(r['supplier_key']==key for r in sample_rows) if key else 0,
             'explicit_classes':';'.join(sorted({v for r in group_rows for v in r['classes'].split(';') if v})),
             'source_status':'PARTIAL_FRESH_PRICE_COVERAGE' if key else 'FRESH_SUPPLIER_SOURCE_NOT_AVAILABLE'})
    csv_save('03_supplier_coverage.csv',coverage,list(coverage[0]))
    summary={'at':now(),'wave_codes':len(codes),'positive_stock_codes':sum(bool(v) for v in by_code.values()),'positive_stock_cards':len(stock),
        'fresh_supplier_price_cards':len(source),'fresh_supplier_site_cards':len(website),
        'fresh_supplier_source_cards':len(source)+len(website),'source_stock_exclusions':dict(Counter(r['status'] for r in observed if r['status']!='CHECKED')),
        'wave_statuses':dict(Counter(r['status'] for r in result)),'supplier_groups':len({r['supplier_group'] for r in stock}),
        'source_confirmed_classes':dict(Counter(r['proposed_class'] for r in result if r['proposed_class'])),
        'current_tc_missing_or_gone':sum(not r['tc_active'] for r in result),'dictionary_rows':len(bank),'external_writes':0,'db_writes':0}
    save('evidence_summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

def kit_families():
    cached=json.loads((OUT/'tc_sets_cache.json').read_text());codes=set(json.loads((OUT/'scope.json').read_text())['codes'])
    touched=set();expanded=set(codes)
    while True:
        previous=len(expanded)
        for r in cached:
            family={r['external_code'],*r['components']}
            if family&expanded:touched.add(r['external_code']);expanded|=family
        if len(expanded)==previous:break
    models={r['external_code']:r for r in json.loads((OUT/'tc_models.json').read_text())}
    touched|={c for c in codes if models.get(c,{}).get('consumable_type') in ('Комплект картриджей','Комплект фотобарабанов','Комплект чернил')}
    endpoint=os.getenv('CARTRIDGE_API_URL','https://thecartridge.ru/api/catalog/mix_data')
    def fetch_set(code):
        path=OUT/f'raw/tc_sets/{code}.json'
        if path.exists():
            cached_result=json.loads(path.read_text())
            if cached_result['status'] in ('LIVE_SET','NOT_MIX','EMPTY'):return cached_result
            save(f'raw/tc_set_retries/{code}_previous.json',cached_result)
        for attempt in range(6):
            try:
                time.sleep(1.5)
                response=requests.post(endpoint,headers={'Api-Key':os.getenv('CARTRIDGE_API_KEY'),'Content-Type':'application/json'},
                     json={'external_code':code},timeout=30)
                if response.status_code==429:
                    time.sleep(min(45,max(15,int(response.headers.get('Retry-After','30')))))
                    continue
                response.raise_for_status();raw=response.json()
                parts=[]
                if isinstance(raw,list):
                    for value in raw:
                        if isinstance(value,dict):value=value.get('external_code')
                        if value is None:raise ValueError('Missing TC component code')
                        parts.append(str(value))
                    state='LIVE_SET' if parts else 'EMPTY'
                elif isinstance(raw,dict) and raw.get('error')=='not_mix':state='NOT_MIX'
                else:state='REVIEW_RESPONSE'
                result={'external_code':code,'components':parts,'status':state,'read_at':now(),'raw':raw}
                save(f'raw/tc_sets/{code}.json',result);return result
            except Exception as error:
                if attempt==5:
                    result={'external_code':code,'components':[],'status':'READ_ERROR','error':str(error),'read_at':now()}
                    save(f'raw/tc_sets/{code}.json',result);return result
                time.sleep(2)
        result={'external_code':code,'components':[],'status':'READ_ERROR','error':'Read rate-limit retries exhausted','read_at':now()}
        save(f'raw/tc_sets/{code}.json',result);return result
    live=[]
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures=[pool.submit(fetch_set,c) for c in sorted(touched)]
        for future in as_completed(futures):
            live.append(future.result())
            if len(live)%25==0:print('TC sets read',len(live),'of',len(touched),flush=True)
    live.sort(key=lambda r:r['external_code']);save('04_live_tc_sets.json',live)
    graph=defaultdict(set)
    for r in live:
        if r['status']!='LIVE_SET':continue
        family={r['external_code'],*r['components']}
        for c in family:graph[c]|=family-{c}
    results={r['external_code']:r for r in json.loads((OUT/'wave2_results.json').read_text())}
    visited=set();plans=[];family_index=0
    for code in sorted(codes):
        if code in visited:continue
        group=set();todo=[code]
        while todo:
            c=todo.pop()
            if c in group:continue
            group.add(c);todo+=list(graph[c]-group)
        visited|=group;family_index+=1
        seeds=[results[c] for c in group if c in results and results[c]['proposed_class']]
        types={r['proposed_class'] for r in seeds};provisional=any(results.get(c,{}).get('status')=='MS_POSITIVE_UNVERIFIED_SOURCE' for c in group)
        value=next(iter(types)) if len(types)==1 else ''
        status='CHIP_TYPE_CONFLICT' if len(types)>1 else 'SOURCE_CONFIRMED_FAMILY' if value else 'MS_ONLY_FAMILY_REVIEW' if provisional else 'NO_CONFIRMED_POSITIVE'
        for c in sorted(group):
            model=models.get(c,{})
            plans.append({'family':family_index,'external_code':c,'in_wave2':c in codes,'tc_title':model.get('title',''),
               'tc_chip':model.get('chip') or '', 'tc_active':bool(model and not model.get('gone_at')),
               'proposed_class':value,'proposed_value':{'chip':'с чипом','chip_free':'с чипом без счётчика'}.get(value,''),
               'status':status,'source_seeds':';'.join(sorted(r['external_code'] for r in seeds)),
               'inherited_without_own_stock':c not in results or not results[c]['positive_supplier_cards'],
               'existing_tc_conflict':bool(value and model.get('chip') and model['chip']!=value)})
    csv_save('04_tc_family_review.csv',plans,list(plans[0]))
    summary={'at':now(),'tc_sets_checked':len(live),'set_statuses':dict(Counter(r['status'] for r in live)),
        'cache_discovery_source':'set_cost: TC mix_data cache; relevant closure refreshed by read-only API',
        'families':family_index,'review_codes':len(plans),'outside_wave_codes':sum(not r['in_wave2'] for r in plans),
        'family_statuses_by_code':dict(Counter(r['status'] for r in plans)),
        'confirmed_family_inactive_tc_codes':sum(r['status']=='SOURCE_CONFIRMED_FAMILY' and not r['tc_active'] for r in plans),
        'external_writes':0,'db_writes':0,'all_TC_sets_globally_refetched':False}
    save('family_summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['collect','names','evidence','kits']);a=p.parse_args()
    if a.stage=='collect':collect()
    elif a.stage=='names':source_names()
    elif a.stage=='evidence':evidence()
    else:kit_families()
