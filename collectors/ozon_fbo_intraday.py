# поток: fin
"""Immutable Ozon stock evidence and conservative order/reserve candidates."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from psycopg2.extras import Json, RealDictCursor, execute_values

from core import db
from collectors.ozon import _headers
from collectors.ozon_fbo_stock import URL
from collectors.wb_fbo_auto_link import _sku_quantity, _contains_sku

ACCOUNTS = ('oz_acc1','oz_acc2')
LOCK_ID = 7348202190


def normalize(items):
    if not isinstance(items,list):
        raise ValueError('invalid_ozon_stock_rows')
    seen, rows = set(), []
    for raw in items:
        sku, warehouse = str(raw.get('sku') or ''), raw.get('warehouse_name')
        if not sku.isdigit() or not isinstance(warehouse,str) or not warehouse.strip():
            raise ValueError('invalid_ozon_stock_identity')
        key = (sku,warehouse)
        if key in seen:
            raise ValueError('duplicate_ozon_stock_identity')
        seen.add(key)
        values=[]
        for field in ('free_to_sell_amount','reserved_amount','promised_amount'):
            value=raw.get(field)
            if isinstance(value,bool) or value is None:
                raise ValueError('incomplete_ozon_stock')
            number=Decimal(str(value))
            if not number.is_finite() or number<0 or number!=int(number):
                raise ValueError('invalid_ozon_stock_quantity')
            values.append(number)
        rows.append((sku,warehouse,*values,Json(raw)))
    return rows


def fetch(account):
    rows=[];seen=set()
    with requests.Session() as client:
        for offset in range(0,100000,1000):
            response=client.post(URL,headers=_headers(account),json={
                'limit':1000,'offset':offset,'warehouse_type':'ALL'},timeout=45)
            response.raise_for_status()
            page=response.json().get('result',{}).get('rows')
            if not isinstance(page,list):
                raise ValueError('incomplete_ozon_stock_response')
            for raw in page:
                key=(str(raw.get('sku')),raw.get('warehouse_name'))
                if key in seen:
                    raise ValueError('repeated_ozon_stock_page')
                seen.add(key)
            rows.extend(page)
            if len(page)<1000:
                return normalize(rows)
    raise ValueError('ozon_stock_page_limit')


def collect(account):
    if account not in ACCOUNTS:
        raise ValueError('unknown_ozon_inventory_account')
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s)',(LOCK_ID+ACCOUNTS.index(account),))
            if not cur.fetchone()[0]:
                return {'busy':True}
            cur.execute('SELECT max(completed_at) FROM ozon_fbo_intraday_capture WHERE account=%s',(account,))
            last=cur.fetchone()[0]
            if last and last>datetime.now(timezone.utc)-timedelta(minutes=25):
                return {'skipped':'recent_capture'}
            start=datetime.now(timezone.utc)
            rows=fetch(account)
            end=datetime.now(timezone.utc)
            cur.execute('''INSERT INTO ozon_fbo_intraday_capture
                (account,started_at,completed_at,source_rows,source_api) VALUES(%s,%s,%s,%s,%s) RETURNING id''',
                (account,start,end,len(rows),'v2/analytics/stock_on_warehouses'))
            cap=cur.fetchone()[0]
            if rows:
                execute_values(cur,'''INSERT INTO ozon_fbo_intraday_item
                    (capture_id,sku,warehouse,free_to_sell,reserved,promised,raw_payload) VALUES %s''',
                    [(cap,*r) for r in rows])
    return {'capture_id':cap,'rows':len(rows),'completed_at':end.isoformat()}


def choose_matches(moves,orders):
    selected=[]
    for move in moves:
        matching=[o for o in orders if move['previous_at']<o['created_at']<=move['current_at']
                  and _contains_sku(o,move['sku'])]
        if len(matching)!=1:
            continue
        order=matching[0];qty=_sku_quantity(order,move['sku'])
        if (qty is None or qty!=move['stock_drop'] or qty!=move['reserve_rise']
                or order.get('mp_status') in ('cancelled','returned')
                or move['promised_before']!=move['promised_after']):
            continue
        # With no order warehouse, require a unique SKU warehouse in BOTH observations.
        if order.get('warehouse'):
            if order['warehouse']!=move['warehouse']:
                continue
        elif move['previous_warehouses']!=1 or move['current_warehouses']!=1:
            continue
        selected.append((move,order,qty))
    counts={}
    for m,o,_ in selected:
        key=(o['id'],m['sku']);counts[key]=counts.get(key,0)+1
    return [(m,o,q) for m,o,q in selected if counts[(o['id'],m['sku'])]==1]


def run(account):
    if account not in ACCOUNTS:
        raise ValueError('unknown_ozon_inventory_account')
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s) locked',(LOCK_ID+10+ACCOUNTS.index(account),))
            if not cur.fetchone()['locked']:
                return {'busy':True}
            cur.execute('''WITH pairs AS (
              SELECT c.id current_id,c.completed_at current_at,p.id previous_id,p.completed_at previous_at
              FROM ozon_fbo_intraday_capture c JOIN LATERAL (
                SELECT * FROM ozon_fbo_intraday_capture p WHERE p.account=c.account
                  AND p.completed_at<c.completed_at ORDER BY p.completed_at DESC LIMIT 1) p ON true
              WHERE c.account=%s AND c.completed_at>=now()-interval '7 days'
                AND c.source_api=p.source_api AND c.completed_at-p.completed_at<=interval '2 hours'),
              items AS(SELECT *,count(*) over(partition by capture_id,sku) warehouses FROM ozon_fbo_intraday_item)
              SELECT p.*,b.sku,b.warehouse,b.free_to_sell-c.free_to_sell stock_drop,
                c.reserved-b.reserved reserve_rise,b.promised promised_before,c.promised promised_after,
                b.warehouses previous_warehouses,c.warehouses current_warehouses
              FROM pairs p JOIN items b ON b.capture_id=p.previous_id
              JOIN items c ON c.capture_id=p.current_id AND c.sku=b.sku AND c.warehouse=b.warehouse
              WHERE b.free_to_sell>c.free_to_sell AND c.reserved>b.reserved''',(account,))
            moves=list(cur.fetchall())
            if not moves:
                return {'moves':0,'candidates':0,'inserted':0}
            cur.execute('''SELECT id,posting_number,created_at,warehouse,products,mp_status FROM mp_fbo_orders
              WHERE platform='ozon' AND account=%s AND created_at>%s AND created_at<=%s''',
              (account,min(m['previous_at'] for m in moves),max(m['current_at'] for m in moves)))
            matches=choose_matches(moves,list(cur.fetchall()));inserted=0
            for move,order,qty in matches:
                evidence=dict(kind='unique_ozon_order_stock_reserve',confidence='inferred_unique',
                    cogs_assigned=False,mp_movement_id=None,source='v2/analytics/stock_on_warehouses',
                    previous_observed_at=move['previous_at'].isoformat(),current_observed_at=move['current_at'].isoformat(),
                    stock_drop=str(move['stock_drop']),reserve_rise=str(move['reserve_rise']),
                    order_warehouse_provided=bool(order.get('warehouse')))
                cur.execute('''INSERT INTO ozon_fbo_stock_link_candidates
                  (account,order_id,sku,source_warehouse,previous_capture_id,current_capture_id,quantity,evidence)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
                  (account,order['id'],move['sku'],move['warehouse'],move['previous_id'],move['current_id'],qty,Json(evidence)))
                inserted+=int(cur.fetchone() is not None)
    return {'moves':len(moves),'candidates':len(matches),'inserted':inserted}
