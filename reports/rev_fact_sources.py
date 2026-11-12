# поток: rev
"""Read-only main-DB bridge; only whitelisted technical facts enter Rev."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from dotenv import dotenv_values
import psycopg2
from psycopg2.extras import Json, RealDictCursor
import requests

from core import db
from reports.prompt_privacy import assert_public_facts

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path('/opt/mp-analytics')
TC_FIELDS = ('external_code','title','additional_title','united_title','similar_title',
             'consumable_type','chip','max_pages','volume_ml','ink_type','ink_colors','colors_count',
             'printer_models','firmware_limit','originality','capacity','incoming_references','outgoing_references')


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def setup():
    db.apply_sql_file(ROOT/'migrations/719_rev_question_evidence.sql')


def save(kind,key,payload,source_at=None):
    assert_public_facts(payload,'technical evidence cache')
    db.execute('''INSERT INTO rev_fact_source(kind,source_key,payload,fingerprint,source_at)
        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(kind,source_key) DO UPDATE SET
        payload=EXCLUDED.payload,fingerprint=EXCLUDED.fingerprint,source_at=EXCLUDED.source_at,checked_at=now()''',
        (kind,str(key),Json(payload),digest(payload),source_at))


def cached(kind,key,max_age_hours=None):
    rows=db.query('''SELECT payload,fingerprint,source_at,checked_at FROM rev_fact_source
        WHERE kind=%s AND source_key=%s AND (%s IS NULL OR checked_at>now()-(%s*interval '1 hour'))''',
        (kind,str(key),max_age_hours,max_age_hours))
    return rows[0] if rows else None


@contextmanager
def primary():
    dsn=dotenv_values(MAIN/'.env').get('DATABASE_URL')
    if not dsn:raise RuntimeError('Main catalogue database is not configured')
    conn=psycopg2.connect(dsn,connect_timeout=8)
    conn.set_session(readonly=True,autocommit=False)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout='20s'")
            yield cur
    finally:
        conn.rollback();conn.close()


def public_tc(raw):
    out={k:raw.get(k) for k in TC_FIELDS if k in raw}
    # Zero in the legacy schema is not a firmware test result.
    if out.get('firmware_limit') in (0,'0','',None):out['firmware_limit']=None
    assert_public_facts(out,'TK technical projection')
    return out


def technical_name(name):
    """No internal title, brand of supplier, prices or stock leave this function."""
    text=str(name or '')
    if re.search(r'внимание|не\s+(?:идет|идёт|подходит)|уцен|брак|пересорт',text,re.I):
        return {'technical_warning':True}
    facts={}
    chip=None
    if re.search(r'без\s+чип|чип\s+отсутств',text,re.I):chip='none'
    elif re.search(r'(?:с|c)\s+чипом|оснащ\w*\s+чип',text,re.I):chip='installed'
    elif re.search(r'чип\s+не\s+требуется|не\s+требуется\s+чип',text,re.I):chip='not_required'
    if chip:facts['chip']=chip
    if re.search(r'без\s+сч[её]тчика',text,re.I):facts['level_counter']='absent'
    count=re.search(r'(?:комплект|набор)\s*(?:из\s*)?(\d{1,2})\s*(?:шт|картридж|цвет)',text,re.I)
    if count:facts['pack_count']=int(count.group(1))
    volume=re.search(r'\b(\d+(?:[.,]\d+)?)\s*мл\b',text,re.I)
    if volume:facts['volume_ml']=volume.group(1).replace(',','.')
    resource=re.search(r'\b(\d[\d ]{1,6})\s*(?:стр\b|страниц)',text,re.I)
    if resource:facts['pages']=resource.group(1).replace(' ','')
    facts['is_set']=bool(re.search(r'комплект|набор',text,re.I))
    return facts


def offer_identity(row):
    platform,account,item=row['platform'],row['account'],str(row.get('item_id') or '')
    if platform=='yandex':return item
    if platform=='wb':
        rows=db.query('SELECT vendor_code code FROM wb_cards WHERE account=%s AND nm_id=%s',(account,item))
    else:
        rows=db.query('SELECT offer_id code FROM raw_ozon_attributes WHERE account=%s AND sku=%s',(account,item))
        if not rows:rows=db.query('SELECT offer_id code FROM ozon_product WHERE account=%s AND sku=%s',(account,item))
    return str(rows[0]['code']) if rows else ''


def resolve(row,native):
    """A prefix only generates a candidate. Exact full model and variant validate it."""
    offer=offer_identity(row)
    prefix=re.match(r'^\d+',offer)
    candidates=list(dict.fromkeys([offer]+([prefix.group()] if prefix else [])))
    hits={code:cached('tk',code,48) for code in candidates}
    records=[{'external_code':code,'raw':hit['payload'],'last_seen':hit['source_at']}
             for code,hit in hits.items() if hit]
    if not records:
        with primary() as cur:
            cur.execute('SELECT external_code,raw,last_seen FROM prc_tc_model WHERE external_code=ANY(%s) AND gone_at IS NULL',(candidates,))
            records=list(cur.fetchall())
    matches=[]
    title=re.sub(r'[^a-zа-я0-9]','',(native.get('name') or '').lower())
    for record in records:
        code=record['external_code'];hit=hits.get(code)
        source=hit['payload'] if hit else public_tc(record['raw'])
        source_date=hit['source_at'] if hit else record['last_seen']
        model=re.sub(r'[^a-zа-я0-9]','',str(source.get('title') or '').lower())
        exact=offer==code
        model_matches=len(model)>=3 and model in title
        if not (exact or model_matches):continue
        set_count=source.get('colors_count') or 0
        native_set=bool(re.search(r'\b(?:комплект|набор)(?:е|а|ом)?\b',
                                 ' '.join(str(native.get(k) or '') for k in ('name','set_info')),re.I))
        if not native_set:
            count=re.search(r'\b([2-9]|\d{2})\s*(?:шт|картридж|цвет)',str(native.get('set_info') or ''),re.I)
            native_set=bool(count and int(count.group(1))==int(set_count or 0))
        if int(set_count or 0)>1 and not native_set:continue
        matches.append({'external_code':code,'proof':'exact_external_code' if exact else 'code_candidate_and_exact_model_and_set',
                        'technical':source,'source_at':str(source_date),'fresh':bool(hit)})
    if len(matches)!=1:return {'offer_id':offer,'status':'ambiguous' if matches else 'unresolved','candidates':len(matches)}
    return dict(matches[0],offer_id=offer,status='matched')


def ms_facts(code):
    with primary() as cur:
        cur.execute('SELECT ms_id,name FROM ms_product WHERE external_code=%s AND NOT archived ORDER BY ms_id LIMIT 20',(code,))
        rows=cur.fetchall()
    return [{'record':str(row['ms_id']),'facts':technical_name(row['name'])} for row in rows]


def refresh_choices(codes):
    """Reuse deployed buying policy in a separate read-only main-DB process."""
    env=os.environ.copy();env['DATABASE_URL']=dotenv_values(MAIN/'.env')['DATABASE_URL']
    env['PGOPTIONS']='-c default_transaction_read_only=on'
    result=subprocess.run([str(MAIN/'venv/bin/python'),str(ROOT/'ops/rev_supplier_facts.py')],
        cwd=MAIN,env=env,input=json.dumps(sorted(set(codes))),capture_output=True,text=True,timeout=90)
    if result.returncode:raise RuntimeError('Supplier policy reader failed; private output suppressed')
    data=json.loads(result.stdout)
    for code,value in data.items():save('purchase_choice',code,value,datetime.now(timezone.utc))
    return len(data)


def refresh_tk(max_pages=400):
    """API has pagination only. Persist resumable 20-row pages, no false filters."""
    setup()
    db.execute("INSERT INTO rev_fact_cursor(name) VALUES('tk') ON CONFLICT DO NOTHING")
    state=db.query("SELECT * FROM rev_fact_cursor WHERE name='tk'")[0]
    if state['completed_at'] and (datetime.now(timezone.utc)-state['completed_at']).total_seconds()<86400:
        return {'fresh':True,'pages':0}
    page=state['page'];seen=set();total=0
    try:
        for _ in range(max_pages):
            response=requests.post('https://thecartridge.ru/api/catalog/cartridge_models',
                headers={'Api-Key':os.environ['CARTRIDGE_API_KEY']},json={'page':page,'limit':20},timeout=35)
            if not response.ok:raise RuntimeError('HTTP_'+str(response.status_code))
            data=response.json()
            if not isinstance(data,list):raise RuntimeError('Invalid catalogue response')
            fingerprint=digest(data)
            if data and fingerprint in seen:raise RuntimeError('Repeated catalogue page')
            seen.add(fingerprint)
            for item in data:
                if item.get('external_code'):
                    save('tk',item['external_code'],public_tc(item),datetime.now(timezone.utc));total+=1
            page+=1
            if len(data)<20:
                db.execute("UPDATE rev_fact_cursor SET page=1,completed_at=now(),last_error=NULL,updated_at=now() WHERE name='tk'")
                return {'complete':True,'models':total,'pages':page-state['page']}
            db.execute("UPDATE rev_fact_cursor SET page=%s,last_error=NULL,updated_at=now() WHERE name='tk'",(page,))
            time.sleep(.25)
    except Exception as error:
        db.execute("UPDATE rev_fact_cursor SET last_error=%s,updated_at=now() WHERE name='tk'",(type(error).__name__,))
        return {'complete':False,'models':total,'page':page,'error':type(error).__name__}
    return {'complete':False,'models':total,'page':page}
