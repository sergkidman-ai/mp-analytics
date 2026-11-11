# поток: fin
"""FBS return -> physical marketplace unit -> FBO order proof and immutable history.

No writes to MoySklad/marketplaces or financial marts. Stock coincidences never
confirm cost. A cost is confirmed only by a unique WB barcode/Ozon exemplar and
a verified original single-unit MS shipment/FIFO report.
"""
import argparse
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
import re
import time

import requests
from psycopg2.extras import Json,RealDictCursor
from core import db,ms_api,ms_api_control
from collectors.mp_fbo_orders import timestamp,MOSCOW

ORGS={'wb_acc1':'a5e7ee84-8d63-41f2-9fae-0be2ef90462b','wb_acc2':'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
ORGS.update(oz_acc1=ORGS['wb_acc1'],oz_acc2=ORGS['wb_acc2'])
LOCK=7348202390


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def hid(value):
    return (value or {}).get('meta',{}).get('href','').rstrip('/').rsplit('/',1)[-1]


def validate_proof(document,report):
    positions=(document.get('positions') or {}).get('rows',[])
    rows=report.get('rows',[])
    if (document.get('applicable') is not True or len(positions)!=1
            or document.get('positions',{}).get('meta',{}).get('size',len(positions))!=1
            or Decimal(str(positions[0].get('quantity',0)))!=1 or len(rows)!=1):
        raise ValueError('source_shipment_not_single_posted_unit')
    costs=rows[0].get('positions',[])
    if len(costs)!=1 or hid(costs[0])!=hid(positions[0].get('assortment')):
        raise ValueError('fifo_position_identity_mismatch')
    amount=Decimal(str(costs[0].get('cost',0)))/100
    if not amount.is_finite() or amount<=0:
        raise ValueError('source_fifo_cost_unknown')
    return dict(demand_id=document['id'],name=document['name'],org=hid(document.get('organization')),
        moment=timestamp(document['moment'],naive_zone=MOSCOW).isoformat(),
        external_code=positions[0]['assortment'].get('externalCode'),cost=str(amount),
        document_updated=document.get('updated'),cost_report_hash=digest(report),quantity=1)


def wb_candidates():
    return db.query("""WITH targets AS MATERIALIZED (
      SELECT o.id,o.platform,o.account,o.posting_number,o.created_at,o.products,
        r.payload->>'shk_id' unit_id,r.payload->>'nm_id' sku,array_agg(r.rrd_id ORDER BY r.rrd_id) target_rrd_ids
      FROM mp_fbo_orders o JOIN raw_wb_report r ON r.account=o.account
        AND r.payload->>'srid'=o.posting_number
      WHERE o.platform='wb' AND coalesce(r.payload->>'shk_id','0') NOT IN ('','0')
      GROUP BY o.id,r.payload->>'shk_id',r.payload->>'nm_id'),
      units AS(SELECT *,count(*) OVER(PARTITION BY id) target_unit_count FROM targets)
      SELECT t.*,s.rrd_id source_rrd_id,s.payload->>'assembly_id' assembly_id,
        s.payload->>'srid' source_srid,s.payload->>'order_dt' source_created_at,
        d.demand_id,d.qty,d.cogs,d.npos
      FROM units t JOIN raw_wb_report s ON s.account=t.account
        AND s.payload->>'shk_id'=t.unit_id AND s.payload->>'nm_id'=t.sku
        AND s.payload->>'srid'<>t.posting_number
      LEFT JOIN ms_demand_cogs d ON d.demand_name=s.payload->>'assembly_id'
        AND d.org=CASE WHEN t.account='wb_acc1' THEN %s ELSE %s END
        AND d.agent='Покупатель ВБ'
      WHERE coalesce(s.payload->>'assembly_id','0') NOT IN ('','0')""",
      (ORGS['wb_acc1'],ORGS['wb_acc2']))


def ozon_candidates():
    return db.query("""WITH unit_rows AS MATERIALIZED (
      SELECT r.account,r.return_id,r.payload,e->>'exemplar_id' unit_id,
        r.payload#>>'{product,sku}' sku FROM raw_mp_returns r
      CROSS JOIN LATERAL jsonb_array_elements(coalesce(r.payload->'exemplars','[]')) e
      WHERE r.platform='ozon' AND coalesce(e->>'exemplar_id','0') NOT IN ('','0')),
      targets AS (
        SELECT o.id,o.platform,o.account,o.posting_number,o.created_at,o.products,
          u.unit_id,u.sku,array_agg(DISTINCT u.return_id ORDER BY u.return_id) target_rrd_ids
        FROM mp_fbo_orders o JOIN unit_rows u ON u.account=o.account
          AND u.payload->>'schema'='Fbo' AND u.payload->>'posting_number'=o.posting_number
        WHERE o.platform='ozon' GROUP BY o.id,u.unit_id,u.sku),
      units AS(SELECT *,count(*) OVER(PARTITION BY id) target_unit_count FROM targets)
      SELECT t.*,s.return_id source_rrd_id,s.payload->>'posting_number' assembly_id,
        s.payload->>'posting_number' source_srid,
        coalesce(s.payload#>>'{logistic,final_moment}',s.payload#>>'{visual,change_moment}') source_created_at,
        d.demand_id,d.qty,d.cogs,d.npos
      FROM units t JOIN unit_rows s ON s.account=t.account AND s.unit_id=t.unit_id AND s.sku=t.sku
        AND s.payload->>'schema'='Fbs' AND s.payload#>>'{visual,status,sys_name}'='ReturnedToOzon'
      LEFT JOIN ms_demand_cogs d ON d.demand_name=s.payload->>'posting_number'
        AND d.org=CASE WHEN t.account='oz_acc1' THEN %s ELSE %s END""",
      (ORGS['oz_acc1'],ORGS['oz_acc2']))


def choose_origins(rows):
    grouped=defaultdict(list)
    for row in rows:grouped[row['id']].append(row)
    chosen={};rejected={}
    for oid,items in grouped.items():
        first=items[0];products=first['products']
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+){0,3}',str(first['assembly_id'])) or str(first['assembly_id'])=='0':
            rejected[oid]='ambiguous_source_document';continue
        if (any(r.get('target_unit_count',1)!=1 for r in items)
                or len({r['unit_id'] for r in items})!=1 or len({r['assembly_id'] for r in items})!=1):
            rejected[oid]='ambiguous_unit_origin';continue
        if (len(products)!=1 or str(products[0].get('sku'))!=first['sku']
                or Decimal(str(products[0].get('quantity',0)))!=1):
            rejected[oid]='ambiguous_market_quantity';continue
        known=[r for r in items if r['demand_id'] and r['cogs'] and r['cogs']>0
               and r['qty']==1 and r['npos']==1]
        if not known and any(r['demand_id'] for r in items):
            rejected[oid]=('source_quantity_needs_verification'
                if any(r['demand_id'] and (r['qty']!=1 or r['npos']!=1) for r in items)
                else 'source_fifo_not_loaded');continue
        if known and len({r['demand_id'] for r in known})!=1:
            rejected[oid]='ambiguous_source_document';continue
        row=dict((known or items)[0],source_rrd_ids=sorted({r['source_rrd_id'] for r in items}))
        dates=[timestamp(r['source_created_at'],naive_zone=MOSCOW) for r in items if r.get('source_created_at')]
        if not dates or min(dates)>=row['created_at']:
            rejected[oid]='source_chronology_unconfirmed';continue
        chosen[oid]=row
    return chosen,rejected


def fetch_proof(demand_id):
    document=ms_get('/entity/demand/'+demand_id,{'expand':'positions.assortment'})
    report=ms_get('/report/stock/byoperation',{'operation.id':demand_id})
    return document,report,validate_proof(document,report)


def ms_get(path,params):
    response=ms_api_control.request("GET",ms_api._url(path,params),sender=requests.get,client="fbo-proof",kind="background",headers={
        'Authorization':'Bearer '+ms_api.TOKEN,'Accept-Encoding':'gzip'},timeout=20)
    response.raise_for_status()
    return response.json()


def event(cur,order_id,kind,key,at,payload):
    cur.execute('''INSERT INTO mp_fbo_trace_event
      (order_id,source_kind,source_id,event_hash,effective_at,payload)
      VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
      (order_id,kind,str(key),digest(payload),at,Json(payload,dumps=lambda v:json.dumps(v,default=str))))
    return int(cur.fetchone() is not None)


def archive_proof(row):
    db.execute('''INSERT INTO mp_fbo_ms_cost_proof_event
      (demand_id,proof_hash,checked_at,document,cost_report,proof) VALUES(%s,%s,%s,%s,%s,%s)
      ON CONFLICT DO NOTHING''',(row['demand_id'],digest({k:row[k] for k in ('document','cost_report','proof')}),
      row['checked_at'],Json(row['document']),Json(row['cost_report']),Json(row['proof'])))


def sync_origins(cur):
    """Record original FBS return lots; these are NOT allocations to FBO orders."""
    cur.execute("SELECT proof FROM mp_fbo_ms_cost_proof WHERE checked_at>now()-interval '1 day'")
    actual_proofs={(r['proof'].get('org'),r['proof'].get('name')):r['proof'] for r in cur.fetchall()}
    cur.execute('''SELECT c.*,ch.result,o.mp_payload FROM wb_return_journey_candidate c
      JOIN customer_guard_orders o ON c.order_kind='fbs' AND o.id=c.order_id
      LEFT JOIN customer_guard_checks ch ON ch.id=o.latest_check_id''')
    origins=[]
    for row in cur.fetchall():
        check=row['result'] or {};products=row['mp_payload'].get('products',[])
        actual=(check.get('cost_kind')=='actual' and check.get('allocation')=='single_market_line'
                and check.get('composition',{}).get('verified') is True and len(products)==1
                and str(products[0].get('sku'))==row['sku'] and Decimal(str(products[0].get('quantity',0)))==1
                and check.get('cost') and Decimal(str(check['cost']))>0)
        origins.append(dict(platform='wb',account=row['account'],source_key='journey:'+str(row['id']),
            fbs_posting_number=row['posting_number'],sku=row['sku'],quantity=1,stage=row['stage'],
            received_at=row['destination_observed_at'],original_cost=check.get('cost') if actual else None,
            cost_kind='actual_fbs_shipment' if actual else 'unconfirmed',
            evidence=dict(journey_id=row['id'],cost_sources=check.get('cost_sources',[]) if actual else [],
                return_evidence=row['evidence'],fbo_cost_assigned=False)))
    cur.execute("""SELECT r.account,r.return_id,r.payload FROM raw_mp_returns r
      WHERE r.platform='ozon' AND r.payload->>'schema'='Fbs' AND (
        r.payload#>>'{visual,status,sys_name}'='ReturnedToOzon' OR EXISTS(
          SELECT 1 FROM mp_fbo_return_origin old WHERE old.platform='ozon'
            AND old.account=r.account AND old.source_key='return:'||r.return_id))""")
    for row in cur.fetchall():
        raw=row['payload'];product=raw.get('product') or {};qty=Decimal(str(product.get('quantity',0)))
        if not product.get('sku') or not raw.get('posting_number') or qty<=0:continue
        status=(raw.get('visual') or {}).get('status',{}).get('sys_name','')
        received=status=='ReturnedToOzon'
        original=actual_proofs.get((ORGS[row['account']],raw['posting_number']))
        if (not original or qty!=1 or len(raw.get('exemplars') or [])!=1
                or original.get('external_code')!=str(product.get('offer_id',''))[:4]):
            original=None
        origins.append(dict(platform='ozon',account=row['account'],source_key='return:'+row['return_id'],
            fbs_posting_number=raw['posting_number'],sku=str(product['sku']),quantity=str(qty),
            stage='received_marketplace' if received else 'ozon:'+status,
            received_at=((raw.get('logistic') or {}).get('final_moment')
              or (raw.get('visual') or {}).get('change_moment')) if received else None,
            original_cost=original['cost'] if original else None,
            cost_kind='actual_fbs_shipment' if original else 'unconfirmed',
            evidence=dict(return_id=row['return_id'],unit_ids=raw.get('exemplars') or [],
                place=raw.get('place') or {},status=status,fbo_cost_assigned=False,
                source_shipment_id=original.get('demand_id') if original else None)))
    for row in origins:
        payload=json.loads(json.dumps(row,default=str))
        cur.execute('''INSERT INTO mp_fbo_return_origin
          (platform,account,source_key,fbs_posting_number,sku,quantity,stage,received_at,original_cost,cost_kind,evidence)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(platform,account,source_key)
          DO UPDATE SET stage=EXCLUDED.stage,received_at=coalesce(EXCLUDED.received_at,mp_fbo_return_origin.received_at),original_cost=EXCLUDED.original_cost,
            cost_kind=EXCLUDED.cost_kind,evidence=EXCLUDED.evidence,updated_at=now()''',
          tuple(row[k] for k in ('platform','account','source_key','fbs_posting_number','sku','quantity','stage','received_at','original_cost','cost_kind'))+(Json(payload['evidence']),))
        cur.execute('''INSERT INTO mp_fbo_return_origin_event(platform,account,source_key,event_hash,payload)
          VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
          (row['platform'],row['account'],row['source_key'],digest(payload),Json(payload)))
    return len(origins)


def run(proof_limit=0):
    with db.get_conn() as lock_conn,lock_conn.cursor() as lock_cur:
        lock_cur.execute('SELECT pg_try_advisory_xact_lock(%s)',(LOCK,))
        if not lock_cur.fetchone()[0]:return {'busy':True}
        chosen,rejected=choose_origins(wb_candidates()+ozon_candidates())
        proofs={r['demand_id']:r for r in db.query('SELECT * FROM mp_fbo_ms_cost_proof')}
        for row in proofs.values():archive_proof(row)
        fetched=errors=attempts=0;now=datetime.now(timezone.utc);deadline=time.monotonic()+150
        lookups={(r['account'],r['fbs_posting_number']):r for r in db.query('SELECT * FROM mp_fbo_source_lookup')}
        groups=defaultdict(list)
        for source in chosen.values():groups[(source['account'],source['assembly_id'])].append(source)
        for (account,assembly),sources in sorted(groups.items(),key=lambda item:(
                not item[1][0]['demand_id'],-max(r['created_at'].timestamp() for r in item[1]),item[0])):
            did=sources[0]['demand_id']
            lookup=lookups.get((account,assembly))
            if not did and lookup:
                did=lookup['demand_id']
                retry_after=timedelta(days=1) if lookup.get('error')=='ValueError' else timedelta(minutes=15)
                if not did and lookup['checked_at']>now-retry_after:continue
            for source in sources:source['demand_id']=did
            if lookup and lookup.get('error'):
                ttl=timedelta(days=1) if lookup['error'] in ('not_unique_source_document',
                    'source_shipment_not_single_posted_unit','fifo_position_identity_mismatch','source_fifo_cost_unknown') else timedelta(minutes=15)
                if lookup['checked_at']>now-ttl:continue
            old=proofs.get(did)
            if old and old['checked_at']>now-timedelta(days=1):continue
            if attempts>=proof_limit or time.monotonic()>deadline:continue
            attempts+=1
            try:
                if not did:
                    docs=ms_get('/entity/demand',{'limit':2,'filter':
                        'name='+assembly+';organization='+ms_api.BASE+'/entity/organization/'+ORGS[account]+';applicable=true'})
                    if docs.get('meta',{}).get('size')!=1 or len(docs.get('rows',[]))!=1:
                        raise ValueError('not_unique_source_document')
                    did=docs['rows'][0]['id']
                    db.execute('''INSERT INTO mp_fbo_source_lookup(account,fbs_posting_number,demand_id)
                      VALUES(%s,%s,%s) ON CONFLICT(account,fbs_posting_number) DO UPDATE SET
                      demand_id=EXCLUDED.demand_id,checked_at=now(),error=NULL''',(account,assembly,did))
                    for source in sources:source['demand_id']=did
                doc,report,proof=fetch_proof(did)
                saved=db.query('''INSERT INTO mp_fbo_ms_cost_proof(demand_id,checked_at,document,cost_report,proof)
                  VALUES(%s,now(),%s,%s,%s) ON CONFLICT(demand_id) DO UPDATE SET
                  checked_at=now(),document=EXCLUDED.document,cost_report=EXCLUDED.cost_report,proof=EXCLUDED.proof
                  RETURNING checked_at''',
                  (did,Json(doc),Json(report),Json(proof)))
                proofs[did]=dict(demand_id=did,document=doc,cost_report=report,proof=proof,checked_at=saved[0]['checked_at'])
                archive_proof(proofs[did]);fetched+=1
                db.execute('''INSERT INTO mp_fbo_source_lookup(account,fbs_posting_number,demand_id)
                  VALUES(%s,%s,%s) ON CONFLICT(account,fbs_posting_number) DO UPDATE SET
                  demand_id=EXCLUDED.demand_id,error=NULL,checked_at=now()''',(account,assembly,did))
                lookups[(account,assembly)]=dict(demand_id=did,error=None,checked_at=now)
            except Exception as exc:
                errors+=1
                code=str(exc) if isinstance(exc,ValueError) and str(exc) in (
                    'not_unique_source_document','source_shipment_not_single_posted_unit',
                    'fifo_position_identity_mismatch','source_fifo_cost_unknown') else type(exc).__name__
                db.execute('''INSERT INTO mp_fbo_source_lookup(account,fbs_posting_number,demand_id,error)
                  VALUES(%s,%s,%s,%s) ON CONFLICT(account,fbs_posting_number) DO UPDATE SET
                  checked_at=now(),error=EXCLUDED.error,demand_id=EXCLUDED.demand_id''',(account,assembly,did,code))
                lookups[(account,assembly)]=dict(demand_id=did,error=code,checked_at=now)
            time.sleep(.1)
        orders=db.query('SELECT id,platform,account,posting_number,products,created_at FROM mp_fbo_orders')
        confirmed=events=0
        with db.get_conn() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
            origins=sync_origins(cur)
            cur.execute('SELECT * FROM mp_fbo_order_events ORDER BY id')
            for row in cur.fetchall():
                payload={k:row[k] for k in ('mp_status','source_status','warehouse','products','mp_price','source_api')}
                events+=event(cur,row['order_id'],'market_status',row['id'],row['status_at'] or row['observed_at'],payload)
            for table,cap_table in (('mp_fbo_intraday_link_candidates','wb_fbo_intraday_capture'),
                                    ('ozon_fbo_stock_link_candidates','ozon_fbo_intraday_capture')):
                cur.execute(f'''SELECT l.*,c.completed_at FROM {table} l JOIN {cap_table} c ON c.id=l.current_capture_id''')
                for row in cur.fetchall():
                    events+=event(cur,row['order_id'],'stock_movement_candidate',table+':'+str(row['id']),
                        row['completed_at'],dict(sku=row['sku'],quantity=str(row['quantity']),
                        warehouse=row['source_warehouse'],evidence=row['evidence']))
            cur.execute('''SELECT c.order_id,c.sku,e.id,e.stage,e.observed_at,e.evidence
              FROM wb_return_journey_candidate c JOIN wb_return_journey_event e ON e.candidate_id=c.id
              WHERE c.order_kind='fbo' ''')
            for row in cur.fetchall():
                events+=event(cur,row['order_id'],'return_stage',row['id'],row['observed_at'],
                    dict(sku=row['sku'],stage=row['stage'],evidence=row['evidence']))
            for order in orders:
                state='unconfirmed';cost=None
                evidence=dict(reason=rejected.get(order['id'],'unit_origin_not_identified'),
                    route='fbs_shipment_return_fbo',cost_kind='unconfirmed')
                if order['platform']=='ozon':evidence['reason']='market_unit_identity_missing'
                source=chosen.get(order['id'])
                if source:
                    cached=proofs.get(source['demand_id']);proof=cached['proof'] if cached else None
                    if proof and cached['checked_at']>now-timedelta(days=1):
                        offer=str(order['products'][0].get('offer_id',''))
                        valid=(proof['name']==source['assembly_id'] and proof['org']==ORGS[order['account']]
                            and proof.get('external_code')==offer[:4]
                            and timestamp(proof['moment'])<order['created_at'])
                        cur.execute('''SELECT 1 FROM ms_return_cogs WHERE demand_name=%s AND org=%s
                          AND store IN ('Звездный','Дисквер','Кантемировская','Брак') AND moment<=%s LIMIT 1''',
                          (source['assembly_id'],proof['org'],order['created_at']))
                        if cur.fetchone():valid=False
                        if valid:
                            state='confirmed';cost=proof['cost'];confirmed+=1
                            evidence=dict(route='fbs_shipment_return_fbo',cost_kind='actual_original_fbs',
                                unit_id=source['unit_id'],sku=source['sku'],source_fbs_posting=source['assembly_id'],
                                source_shipment_id=source['demand_id'],cost_report_hash=proof['cost_report_hash'],
                                source_document_moment=proof['moment'],verified_at=cached['checked_at'].isoformat(),
                                unit_kind='wb_shk' if order['platform']=='wb' else 'ozon_exemplar',
                                method='same_unit_and_original_shipment_fifo')
                            prefix='rrd' if order['platform']=='wb' else 'return'
                            evidence['source_'+prefix+'_ids']=source['source_rrd_ids']
                            evidence['target_'+prefix+'_ids']=source['target_rrd_ids']
                            events+=event(cur,order['id'],'fbs_origin',source['demand_id'],timestamp(proof['moment']),
                                dict(fbs_posting=source['assembly_id'],shipment_id=source['demand_id'],
                                     cost=proof['cost'],unit_id=source['unit_id'],sku=source['sku'],
                                     **({'unit_kind':'ozon_exemplar'} if order['platform']=='ozon' else {})))
                        else:evidence['reason']='source_document_identity_or_return_conflict'
                    else:
                        evidence['reason']='original_ms_fifo_verification_pending'
                        lookup=lookups.get((order['account'],source['assembly_id']))
                        if lookup and lookup.get('error'):
                            evidence.update(reason='original_ms_fifo_verification_failed',proof_error=lookup['error'])
                payload=dict(state=state,cost=str(cost) if cost is not None else None,evidence=evidence)
                version=digest(payload)
                cur.execute('''INSERT INTO mp_fbo_trace_state(order_id,state,cost,evidence,version_hash)
                  VALUES(%s,%s,%s,%s,%s) ON CONFLICT(order_id) DO UPDATE SET state=EXCLUDED.state,
                  cost=EXCLUDED.cost,evidence=EXCLUDED.evidence,version_hash=EXCLUDED.version_hash,updated_at=now()
                  WHERE mp_fbo_trace_state.version_hash<>EXCLUDED.version_hash''',
                  (order['id'],state,cost,Json(evidence),version))
                events+=event(cur,order['id'],'cost_assessment','assessment',datetime.now(timezone.utc),payload)
        return dict(orders=len(orders),origins=origins,confirmed_cost=confirmed,
                    proofs_fetched=fetched,proof_errors=errors,history_events_added=events)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof-limit',type=int,default=0)
    args=parser.parse_args()
    if not 0<=args.proof_limit<=100:parser.error('proof-limit must be 0..100')
    print(json.dumps(run(args.proof_limit),ensure_ascii=False),flush=True)
