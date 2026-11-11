# поток: inv
"""Sticky protection for manually edited customer-order composition.
MS audit is authoritative. UI comments/status edits do not lock composition.
No writes to MS; local evidence survives later automatic replacements.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

PROTECTION_DIR = Path(__file__).resolve().parents[1] / '.private/customer_order_manual_locks'
AUTOMATIC_SOURCES = {'remap-1.2','jsonapi','scriptor','system'}
USER_SOURCES = {'app','mobile','mobileapp','android','ios'}
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
COMPOSITION_FIELDS = {'assortment','quantity','price','discount','vat','vatEnabled','pack'}


class ManualCompositionProtected(ValueError):
    pass


class ManualCompositionUnverified(ValueError):
    pass


def object_id(value):
    value=value or {}
    return (value.get('id') or value.get('meta',{}).get('href','').split('?')[0].rstrip('/').rsplit('/',1)[-1])


def position_signature(rows):
    result=Counter()
    for row in rows:
        if not isinstance(row,dict):raise ManualCompositionUnverified('positions_schema_unverified')
        if not COMPOSITION_FIELDS.intersection(row):continue
        assortment=row.get('assortment') or {}
        pack=row.get('pack')
        result[(object_id(assortment),assortment.get('meta',{}).get('type','product'),
                Decimal(str(row['quantity'])) if row.get('quantity') is not None else None,
                Decimal(str(row['price'])) if row.get('price') is not None else None,
                Decimal(str(row.get('discount') or 0)),
                str(row.get('vat')) if row.get('vat') is not None else None,
                row.get('vatEnabled',True),json.dumps(pack,sort_keys=True,default=str) if pack else None)]+=1
    return result


def composition_changed(event):
    diff=event.get('diff') or {}
    if 'positions' not in diff:return False
    changes=diff['positions']
    if not isinstance(changes,list):raise ManualCompositionUnverified('position_audit_schema_unverified')
    before=[];after=[]
    for change in changes:
        if not isinstance(change,dict):raise ManualCompositionUnverified('position_audit_schema_unverified')
        for key,dest in [('oldValue',before),('newValue',after)]:
            value=change.get(key)
            if value is None:continue
            if not isinstance(value,dict):raise ManualCompositionUnverified('position_audit_value_unverified')
            dest.append(value)
    return position_signature(before)!=position_signature(after)


def all_rows(read,path):
    rows=[];offset=0;total=None
    while True:
        data=read(path,{'limit':20,'offset':offset})
        size=data.get('meta',{}).get('size');page=data.get('rows')
        if not isinstance(size,int) or not isinstance(page,list) or size<0:
            raise ManualCompositionUnverified('audit_collection_unverified')
        if total is not None and total!=size:raise ManualCompositionUnverified('audit_changed_during_scan')
        total=size;rows.extend(page);offset+=len(page)
        if offset>=size:break
        if not page or offset>5000:raise ManualCompositionUnverified('audit_incomplete')
    if len(rows)!=total:raise ManualCompositionUnverified('audit_size_mismatch')
    return rows


def evidence(order,read):
    oid=order['id'];history=all_rows(read,'/entity/customerorder/'+oid+'/audit')
    if not history:raise ManualCompositionUnverified('order_audit_missing')
    for summary in history:
        if summary.get('eventType')!='update':continue
        source=str(summary.get('source') or '').lower()
        if source in AUTOMATIC_SOURCES:continue
        if 'diff' in summary:events=[summary]
        else:
            href=summary.get('audit',{}).get('meta',{}).get('href')
            if not href:raise ManualCompositionUnverified('audit_event_link_missing')
            events=all_rows(read,href.rstrip('/')+'/events')
        matching=[event for event in events if object_id(event.get('entity'))==oid]
        if not matching:raise ManualCompositionUnverified('order_audit_event_missing')
        for event in matching:
            if not composition_changed(event):continue
            if source not in USER_SOURCES:
                raise ManualCompositionUnverified('composition_actor_unverified')
            return dict(order_id=oid,order_name=order.get('name'),reason='manual_user_composition',
                        moment=event.get('moment') or summary.get('moment'),
                        source=source,uid=event.get('uid') or summary.get('uid'),
                        audit=summary.get('audit'),diff=event['diff']['positions'])
    # Never authorize a replacement while the newest edit has not reached audit yet.
    try:
        latest=max(datetime.fromisoformat(row['moment']) for row in history)
        updated=datetime.fromisoformat(order['updated'])
    except (KeyError,ValueError,TypeError):
        raise ManualCompositionUnverified('audit_freshness_unverified') from None
    if latest<updated:raise ManualCompositionUnverified('audit_not_caught_up')
    return None


def remember(proof):
    PROTECTION_DIR.mkdir(parents=True,exist_ok=True)
    target=PROTECTION_DIR/(proof['order_id']+'.json')
    temp=PROTECTION_DIR/(proof['order_id']+'.'+uuid4().hex+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(proof,stream,ensure_ascii=False,default=str)
    os.replace(temp,target)


def assert_automatic_composition(order,read=None):
    if not re.fullmatch(UUID,str(order.get('id') or '')):
        raise ManualCompositionUnverified('existing_order_identity_unverified')
    flag=PROTECTION_DIR/(order['id']+'.json')
    if flag.exists():raise ManualCompositionProtected('manual_user_composition_protected:'+str(order.get('name') or order['id']))
    if read is None:
        from core import ms_api
        read=ms_api.get
    try:proof=evidence(order,read)
    except ManualCompositionUnverified:raise
    except Exception as exc:
        raise ManualCompositionUnverified('manual_history_unavailable:'+type(exc).__name__) from None
    if proof:
        remember(proof)
        raise ManualCompositionProtected('manual_user_composition_protected:'+str(order.get('name') or order['id']))


def position_rows(value):
    if isinstance(value,list):return value
    if isinstance(value,dict) and isinstance(value.get('rows'),list):
        if value.get('meta',{}).get('size',len(value['rows']))!=len(value['rows']):
            raise ManualCompositionUnverified('current_positions_incomplete')
        return value['rows']
    raise ManualCompositionUnverified('current_positions_unverified')


def targets(method,path,payload):
    path=urlsplit(path).path
    match=re.fullmatch(r'/entity/customerorder/('+UUID+r')(/positions(?:/[^/]+)?)?/?',path)
    if match:
        if match[2]:
            values=payload if isinstance(payload,list) else [payload] if isinstance(payload,dict) else []
            if method=='DELETE' or any(COMPOSITION_FIELDS.intersection(v) for v in values):
                return [(match[1],None,True)]
            return []
        return [(match[1],payload['positions'],False)] if isinstance(payload,dict) and 'positions' in payload else []
    if path.rstrip('/')=='/entity/customerorder' and method=='POST':
        values=payload if isinstance(payload,list) else [payload] if isinstance(payload,dict) else []
        result=[]
        for value in values:
            oid=object_id(value)
            if re.fullmatch(UUID,str(oid or '')) and 'positions' in value:result.append((oid,value['positions'],False))
        return result
    return []


def same_composition(current, proposed):
    before=position_rows(current)
    after=position_rows(proposed)
    known={object_id(row):row for row in before if object_id(row)}
    # Existing line IDs allow reserve-only partial rows without mistaking them for deletion.
    keys=[object_id(row) for row in after]
    if len(known)==len(before)==len(after) and len(set(keys))==len(keys) and set(keys)==set(known):
        after=[dict(known[object_id(row)],**row) for row in after]
    return position_signature(before)==position_signature(after)


def assert_write_allowed(method,path,payload,read):
    if method not in ('PUT','POST','DELETE'):return
    for oid,proposed,partial in targets(method,path,payload):
        try:
            order=read('/entity/customerorder/'+oid,{'expand':'positions.assortment'})
        except Exception as exc:
            create_with_new_id=(method=='POST' and urlsplit(path).path.rstrip('/')=='/entity/customerorder')
            if create_with_new_id and getattr(exc,'status',None)==404 and not (PROTECTION_DIR/(oid+'.json')).exists():
                continue
            raise ManualCompositionUnverified('current_order_unavailable:'+type(exc).__name__) from None
        if order.get('id')!=oid:raise ManualCompositionUnverified('order_identity_changed')
        unchanged=not partial and same_composition(order.get('positions'),proposed)
        if not unchanged:
            assert_automatic_composition(order,read)
        # Even reserve-only full rows must not overwrite a composition edited in the meantime.
        after=read('/entity/customerorder/'+oid)
        if after.get('id')!=oid or after.get('updated')!=order.get('updated'):
            raise ManualCompositionUnverified('order_changed_before_composition_write')
