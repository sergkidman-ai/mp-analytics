# поток: fin
"""Read-only WB FBS order feed. POST is used exclusively to READ statuses."""
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
import requests
from shipment_guard.ms import SourceError
from shipment_guard.economics import number
from customer_guard.domain import ACTIVE, TERMINAL

ACCOUNT = 'wb_acc2'
BASE = 'https://marketplace-api.wildberries.ru'
CANCELLED = {'canceled','canceled_by_client','declined_by_client','defect','canceled_by_carrier'}
TRANSIT = {'sorted','ready_for_pickup','postponed_delivery','accepted_by_carrier','sent_to_carrier'}


def status(s):
    supplier, wb = s.get('supplierStatus'), s.get('wbStatus')
    if wb in CANCELLED or supplier in {'cancel','cancel_carrier'}:
        return 'cancelled'
    if wb == 'sold':
        return 'delivered'
    if wb in TRANSIT:
        return 'delivering'
    if wb == 'waiting':
        return {'new':'awaiting_packaging','confirm':'awaiting_deliver','complete':'delivering'}.get(supplier,'unknown')
    return 'unknown'


def normalize(order, state):
    oid = order.get('id')
    if isinstance(oid,bool) or not isinstance(oid,int) or oid<=0 or state.get('id')!=oid:
        raise SourceError('wb_order_identity_mismatch')
    if str(order.get('currencyCode')) == '643':
        price = number(order.get('price'))/100
    elif str(order.get('convertedCurrencyCode')) == '643':
        price = number(order.get('convertedPrice'))/100
    else:
        raise SourceError('wb_non_rub')
    if price <= 0 or not order.get('article') or not order.get('createdAt'):
        raise SourceError('wb_invalid_order_price_or_article')
    stamp = datetime.fromisoformat(order['createdAt'].replace('Z','+00:00'))
    if stamp.tzinfo is None:
        raise SourceError('wb_order_time_without_zone')
    # No recipient, address, phone or buyer comment is stored.
    raw = {k:order[k] for k in ('id','rid','orderUid','article','nmId','createdAt','deliveryType',
        'price','convertedPrice','currencyCode','convertedCurrencyCode') if k in order}
    return dict(platform='wb',account=ACCOUNT,posting_number=str(oid),order_number=str(order.get('orderUid') or oid),
        status=status(state),substatus=f"{state.get('supplierStatus')}:{state.get('wbStatus')}",
        created_at=stamp.isoformat(),in_process_at=stamp.isoformat(),market_order=raw,market_status=state,
        products=[dict(offer_id=order['article'],sku=order.get('nmId'),name=order['article'],quantity=1,price=str(price))])


class WBClient:
    def __init__(self, request=None, clock=time.monotonic, sleep=time.sleep, budget=120):
        self.request = request or requests.request
        self.clock,self.sleep = clock,sleep
        self.deadline=clock()+120
        self.budget,self.calls,self.next_at=budget,0,0

    def call(self, path, *, params=None, body=None):
        if path not in ('/api/v3/orders','/api/v3/orders/new','/api/v3/orders/status'):
            raise SourceError('wb_read_endpoint_not_allowed')
        if (path=='/api/v3/orders/status') != (body is not None):
            raise SourceError('wb_read_method_not_allowed')
        if self.calls>=self.budget or self.clock()>=self.deadline:
            raise SourceError('wb_request_budget',600)
        ref=os.getenv('SHIPMENT_GUARD_WB_TOKEN_ENV_ACC2','WB_TOKEN_ACC2')
        if not re.fullmatch(r'WB_TOKEN_[A-Z_]*ACC2',ref):
            raise SourceError('wb_token_reference_invalid')
        token=os.getenv('SHIPMENT_GUARD_WB_TOKEN_ACC2') or os.getenv(ref)
        if not token:
            raise SourceError('wb_marketplace_token_missing',3600)
        self.sleep(max(0,self.next_at-self.clock()));self.next_at=self.clock()+1
        self.calls+=1
        try:
            response=self.request('POST' if body is not None else 'GET',BASE+path,
                headers={'Authorization':token},params=params,json=body,timeout=(5,25))
        except requests.RequestException:
            raise SourceError('wb_network_error',600) from None
        if response.status_code!=200:
            raise SourceError('wb_http_'+str(response.status_code),3600 if response.status_code in (401,403) else 600)
        try: data=response.json()
        except ValueError: raise SourceError('wb_invalid_json',600) from None
        if not isinstance(data,dict) or not isinstance(data.get('orders'),list):
            raise SourceError('wb_order_schema',600)
        return data

    def statuses(self, ids):
        result={}
        for pos in range(0,len(ids),100):
            part=ids[pos:pos+100]
            rows=self.call('/api/v3/orders/status',body={'orders':part})['orders']
            if len(rows)!=len(part) or {r.get('id') for r in rows}!=set(part):
                raise SourceError('wb_partial_status_response',600)
            result.update({r['id']:r for r in rows})
        return result

    def fetch(self, start, end, max_pages=100):
        orders={};cursor=0
        for _ in range(max_pages):
            data=self.call('/api/v3/orders',params={'limit':20,'next':cursor,
                'dateFrom':int(start.timestamp()),'dateTo':int(end.timestamp())})
            page=data['orders']
            for row in page:orders[row['id']]=row
            if len(page)<20:
                break
            nxt=data.get('next')
            if not isinstance(nxt,int) or nxt<=cursor:
                raise SourceError('wb_cursor_stalled',600)
            cursor=nxt
        else:raise SourceError('wb_page_budget',600)
        for row in self.call('/api/v3/orders/new')['orders']:
            orders[row['id']]=row
        statuses=self.statuses(list(orders))
        return [normalize(row,statuses[oid]) for oid,row in orders.items()]

    def refresh_posts(self, posts):
        ids=[int(p['posting_number']) for p in posts]
        states=self.statuses(ids)
        return [normalize(p['market_order'],states[int(p['posting_number'])]) for p in posts]
