# поток: fin
"""Read-only business orders API, seller-warehouse FBS only; no buyer data retained."""
import os
import time
from dateutil.parser import isoparse
from decimal import Decimal
import requests
from shipment_guard.ms import SourceError
from shipment_guard.economics import number

STATUS = {'PROCESSING':'awaiting_packaging', 'DELIVERY':'delivering', 'PICKUP':'delivering',
          'DELIVERED':'delivered', 'CANCELLED':'cancelled'}


def normalize(order):
    oid=order.get('orderId')
    if isinstance(oid,bool) or not isinstance(oid,int) or oid<=0:
        raise SourceError('yandex_order_identity')
    if order.get('programType')!='FBS' or order.get('fake') is not False:
        raise SourceError('yandex_order_scope')
    stamp=isoparse(order['creationDate'])
    if stamp.tzinfo is None: raise SourceError('yandex_order_time_without_zone')
    products=[];items=[]
    for item in order.get('items',[]):
        qty=number(item.get('count'));prices=item.get('prices') or {}
        if qty<=0 or not item.get('offerId') or 'payment' not in prices:
            raise SourceError('yandex_item_schema')
        total=Decimal(0)
        for key in ('payment','subsidy','cashback'):
            value=prices.get(key)
            if value is None: continue
            if value.get('currencyId') not in ('RUR','RUB'):
                raise SourceError('yandex_non_rub')
            amount=number(value.get('value'))
            if amount<0: raise SourceError('yandex_negative_price')
            total+=amount
        if total<=0: raise SourceError('yandex_invalid_price')
        products.append(dict(offer_id=item['offerId'],quantity=str(qty),price=str(total/qty),name=item.get('offerName') or item['offerId']))
        items.append({k:item[k] for k in ('offerId','offerName','count','prices') if k in item})
    if not products: raise SourceError('yandex_empty_order')
    raw={k:order[k] for k in ('orderId','campaignId','programType','creationDate','fake','status','substatus') if k in order}
    raw['items']=items
    state=STATUS.get(order.get('status'),'unknown')
    if order.get('status')=='PROCESSING' and order.get('substatus')=='READY_TO_SHIP': state='awaiting_deliver'
    return dict(platform='yandex',account='ya_acc1',posting_number=str(oid),order_number=str(oid),
        status=state,substatus=order.get('substatus'),created_at=stamp.isoformat(),in_process_at=stamp.isoformat(),
        products=products,market_order=raw,market_status={k:order.get(k) for k in ('status','substatus')})


class YandexClient:
    def __init__(self, request=None, clock=time.monotonic, sleep=time.sleep, budget=60):
        self.request=request or requests.post
        self.clock,self.sleep=clock,sleep
        self.deadline=clock()+120
        self.calls,self.budget,self.next_at=0,budget,0

    def call(self, body, token=None):
        if self.calls>=self.budget or self.clock()>=self.deadline:
            raise SourceError('yandex_request_budget',600)
        biz=os.environ['YANDEX_BUSINESS_ID_ACC1']
        if not biz.isdigit(): raise SourceError('yandex_business_identity')
        self.sleep(max(0,self.next_at-self.clock()));self.next_at=self.clock()+1;self.calls+=1
        try:
            r=self.request(f'https://api.partner.market.yandex.ru/v1/businesses/{biz}/orders',
                headers={'Api-Key':os.environ['YANDEX_API_KEY_ACC1']},
                json=dict(body,programTypes=['FBS'],fake=False),
                params=dict(limit=20,**({'pageToken':token} if token else {})),timeout=(5,25))
        except requests.RequestException: raise SourceError('yandex_network_error',600) from None
        if r.status_code!=200: raise SourceError('yandex_http_'+str(r.status_code),600)
        try: data=r.json()
        except ValueError: raise SourceError('yandex_invalid_json',600) from None
        if not isinstance(data,dict) or not isinstance(data.get('orders'),list):
            raise SourceError('yandex_orders_schema',600)
        return data

    def fetch(self, start, end, max_pages=50, active_only=False):
        body={'dates':{'creationDateFrom':start.date().isoformat(),'creationDateTo':end.date().isoformat()}}
        if active_only: body['statuses']=['PROCESSING','DELIVERY','PICKUP','UNPAID','PLACING','RESERVED']
        token=None;seen=set();posts={}
        for _ in range(max_pages):
            data=self.call(body,token)
            for order in data['orders']:
                p=normalize(order);oid=p['posting_number']
                if oid in posts and posts[oid]!=p: raise SourceError('yandex_duplicate_order')
                posts[oid]=p
            token=(data.get('paging') or {}).get('nextPageToken')
            if not token: return list(posts.values())
            if token in seen: raise SourceError('yandex_cursor_stalled',600)
            seen.add(token)
        raise SourceError('yandex_page_budget',600)

    def get_post(self, post):
        data=self.call({'orderIds':[int(post['posting_number'])]})
        orders=data['orders']
        if len(orders)!=1 or str(orders[0].get('orderId'))!=post['posting_number']:
            raise SourceError('yandex_order_not_found')
        value=normalize(orders[0])
        if value['market_order']['campaignId']!=post['market_order']['campaignId']:
            raise SourceError('yandex_campaign_changed')
        return value
