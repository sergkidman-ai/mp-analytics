# поток: inv
"""Versioned closure of expense statements and allocations, per legal entity/month."""
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import tempfile
from decimal import Decimal
from zoneinfo import ZoneInfo
from psycopg2.extras import Json,RealDictCursor,execute_values
from core import db

ORGS={'7807355364','7811803918'}


def current_month():
    return dt.datetime.now(ZoneInfo('Europe/Moscow')).date().replace(day=1)


def month_date(value):
    return dt.date.fromisoformat(str(value)[:7]+'-01')


def active(org,month):
    rows=db.query('select id,closed_at,snapshot_sha256 from opex_month_closure where org_inn=%s and month=%s and reopened_at is null',(org,month_date(month)))
    if not rows:return None
    r=rows[0]
    return {'closed':True,'close_id':r['id'],'closed_at':r['closed_at'].isoformat(),'snapshot_sha256':r['snapshot_sha256']}


def summary(org,month):
    rows=db.query('select id,closed_at,summary from opex_month_closure where org_inn=%s and month=%s and reopened_at is null',(org,month_date(month)))
    if not rows:return None
    r=rows[0];payload=copy.deepcopy(r['summary'])
    payload.update(closed=True,close_id=r['id'],closed_at=r['closed_at'].isoformat())
    return payload


def late_count(close_id):
    return db.query('''select count(*)::int n from bank_txn t join opex_month_closure h
     on h.org_inn=t.org_inn and h.month=date_trunc('month',t.operation_date)::date
     where h.id=%s and not exists(select 1 from opex_closed_txn s where s.close_id=h.id and s.txn_id=t.id)''',(close_id,))[0]['n']


def statement(org,month,bank='',direction='DEBIT',cat='',only=''):
    rows=db.query('select id,closed_at,statement from opex_month_closure where org_inn=%s and month=%s and reopened_at is null',(org,month_date(month)))
    if not rows:return None
    r=rows[0];saved=r['statement']
    body=copy.deepcopy(saved['banks'].get(bank,saved['banks']['']))
    if bank and bank not in saved['banks']:
        body['items']=[];body['totals']={};body['by_cat']=[]
    items=body['items']
    if direction:items=[i for i in items if i['direction']==direction]
    if only=='unassigned' or cat=='none':items=[i for i in items if i['category_id'] is None]
    elif str(cat).isdigit():items=[i for i in items if i['category_id']==int(cat)]
    for item in items:item['edit_locked']=True
    body.update(items=items,bank=bank,cat=cat,only=only,closed=True,closed_at=r['closed_at'].isoformat(),close_id=r['id'],late_transactions=late_count(r['id']))
    # Navigation may gain new months; the historical payment data stays unchanged.
    body['months']=[x['m'] for x in db.query('''select distinct m from (
      select date_trunc('month',operation_date)::date::text m from bank_txn where org_inn=%s
      union select month::text from opex_month_closure where org_inn=%s and reopened_at is null) q order by m desc''',(org,org))]
    return body


def locked(txn_id,start=None,n=None):
    row=db.query('''select opex_assignment_locked(t.id,a.start_month,a.spread_months) old_lock,
     opex_assignment_locked(t.id,%s,%s) new_lock from bank_txn t left join bank_txn_opex a on a.txn_id=t.id where t.id=%s''',(start,n,txn_id))
    return bool(row and (row[0]['old_lock'] or row[0]['new_lock']))


def _money(items):
    return float(sum((Decimal(str(i['amount'])) for i in items),Decimal(0)))


def bank_variants(body):
    variants={'':copy.deepcopy(body)}
    flags={c['category_id']:c for c in body['by_cat']}
    for bank in sorted({i['bank'] for i in body['items']}):
        b=copy.deepcopy(body);b['items']=[i for i in body['items'] if i['bank']==bank]
        deb=[i for i in b['items'] if i['direction']=='DEBIT'];assigned=[i for i in deb if i['category_id'] is not None]
        inside=[i for i in assigned if flags[i['category_id']]['in_opex']]
        outside=[i for i in assigned if flags[i['category_id']]['in_opex'] is False]
        b['totals']={'n':len(deb),'n_done':len(assigned),'total':_money(deb),'done':_money(inside),
                     'outside':_money(outside),'n_outside':len(outside),'todo':_money([i for i in deb if i['category_id'] is None])}
        b['by_cat']=[]
        for category in body['by_cat']:
            part=[i for i in assigned if i['category_id']==category['category_id']]
            if part:b['by_cat'].append({**category,'n':len(part),'amount':_money(part)})
        variants[bank]=b
    return variants


def close_many(pairs,builder=None):
    pairs=sorted(set((org,month_date(month)) for org,month in pairs))
    if any(org not in ORGS or month>=current_month() for org,month in pairs):
        raise ValueError('Закрывать можно только прошедшие месяцы своей организации.')
    if builder is None:
        from web.app import opex_statement
        builder=lambda org,month:opex_statement(month=str(month),org=org,direction='')
    results=[]
    with db.get_conn() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('select pg_advisory_xact_lock(23020261110)')
        # Reads remain available; writes finish before a consistent snapshot is captured.
        cur.execute('lock table bank_txn,bank_txn_opex,opex_category,opex_rule in share mode')
        cur.execute('select r.*,c.name category from opex_rule r join opex_category c on c.id=r.category_id order by r.id')
        rules=cur.fetchall()
        for org,month in pairs:
            cur.execute('select id from opex_month_closure where org_inn=%s and month=%s and reopened_at is null',(org,month))
            existing=cur.fetchone()
            if existing:
                results.append({'org':org,'month':str(month),'id':existing['id'],'already_closed':True});continue
            body=builder(org,month)
            cur.execute('select * from opex_fact_alloc_live where org_inn=%s and month=%s order by txn_id',(org,month));alloc=cur.fetchall()
            cur.execute('''select category_id,category,sum(amount)::float amount,count(*)::int txn_count
              from opex_fact_alloc_live where org_inn=%s and month=%s group by 1,2 order by 3 desc''',(org,month))
            items=cur.fetchall();tot=body['totals']
            cost={'items':items,'total':round(sum(i['amount'] for i in items),2),
                  'unassigned':{'n':tot.get('n',0)-tot.get('n_done',0),'s':tot.get('todo',0)}}
            saved={'banks':bank_variants(body)}
            encoded=json.dumps({'statement':saved,'summary':cost,'alloc':alloc,'rules':rules},default=str,sort_keys=True,ensure_ascii=False)
            digest=hashlib.sha256(encoded.encode()).hexdigest()
            cur.execute('''insert into opex_month_closure(org_inn,month,statement,summary,rules_snapshot,snapshot_sha256)
             values(%s,%s,%s,%s,%s,%s) returning id''',(org,month,Json(saved),Json(cost),Json(rules,dumps=lambda v:json.dumps(v,default=str)),digest))
            close_id=cur.fetchone()['id']
            flags={c['category_id']:c['in_opex'] for c in body['by_cat']}
            txns=[(close_id,i['id'],i['category_id'],i['category'],flags.get(i['category_id']),i['source'],i['start_month'],i['spread_months']) for i in body['items']]
            if txns:execute_values(cur,'''insert into opex_closed_txn(close_id,txn_id,category_id,category,in_opex,source,start_month,spread_months) values %s''',txns)
            if alloc:execute_values(cur,'insert into opex_closed_alloc(close_id,txn_id,payload) values %s',[(close_id,a['txn_id'],Json(a,dumps=lambda v:json.dumps(v,default=str))) for a in alloc])
            if items:execute_values(cur,'insert into opex_closed_item(close_id,category_id,category,amount,txn_count) values %s',[(close_id,i['category_id'],i['category'],i['amount'],i['txn_count']) for i in items])
            results.append({'org':org,'month':str(month),'id':close_id,'total':cost['total'],'unassigned':cost['unassigned'],'snapshot_sha256':digest})
    for result in results:
        if not result.get('already_closed'):
            try:
                export_snapshot(result['id'])
                result['archived']=True
            except OSError as exc:
                result['archived']=False
                result['archive_error']=type(exc).__name__
    return results


def export_snapshot(close_id):
    header=db.query('select * from opex_month_closure where id=%s',(close_id,))[0]
    content={'closure':header}
    for table in ['opex_closed_txn','opex_closed_alloc','opex_closed_item']:
        order='category_id' if table=='opex_closed_item' else 'txn_id'
        content[table]=db.query('select * from '+table+' where close_id=%s order by '+order,(close_id,))
    root=pathlib.Path(os.getenv('OPEX_CLOSE_ARCHIVE_DIR','/opt/mp-analytics/.private/opex_closed_months'))
    directory=root/header['org_inn']
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/(str(header['month'])+'_v'+str(close_id)+'.json')
    with tempfile.NamedTemporaryFile(dir=directory,prefix='.snapshot-',delete=False) as f:
        f.write(json.dumps(content,default=str,ensure_ascii=False,sort_keys=True,indent=2).encode())
        f.flush();os.fsync(f.fileno());tmp=pathlib.Path(f.name)
    try:tmp.replace(target)
    finally:tmp.unlink(missing_ok=True)
    return target


def reopen(org,month):
    if org not in ORGS:raise ValueError('Неизвестная организация')
    with db.get_conn() as conn,conn.cursor() as cur:
        cur.execute('select pg_advisory_xact_lock(23020261110)')
        cur.execute('''update opex_month_closure set reopened_at=now() where org_inn=%s and month=%s and reopened_at is null returning id''',(org,month_date(month)))
        row=cur.fetchone()
    return {'ok':True,'reopened':bool(row)}
