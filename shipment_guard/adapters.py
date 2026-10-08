# поток: fin
"""Narrow, read-only order confirmation. No catalogue/list-price substitutes."""
import os
import re
import time
from collections import defaultdict
from decimal import Decimal
import requests
from core import db
from shipment_guard import config, store
from shipment_guard.economics import number, money
from shipment_guard.ms import SourceError, retry_seconds

class OrderAdapter:
    def __init__(self):
        self.next_at={}
        self.unavailable={}
        self.wb_cache={}
        self.pulse=lambda:None

    def request(self, platform, account, method, url, **kwargs):
        key=(platform,account)
        if self.unavailable.get(key,0)>time.monotonic():
            raise SourceError(platform+'_access_unavailable')
        while self.next_at.get(key,0)>time.monotonic():
            self.pulse(); time.sleep(min(10,self.next_at[key]-time.monotonic()))
        self.pulse()
        # Strictly serial; WB 10/min, Ozon/Market 1/s, with server pauses respected.
        self.next_at[key]=time.monotonic()+(6 if platform=='wb' else 1)
        try: r=requests.request(method,url,timeout=(5,30),**kwargs)
        except requests.RequestException: raise SourceError(platform+'_network_error') from None
        store.metric(platform+'_requests',details={'last_status':r.status_code})
        if r.status_code==429:
            self.next_at[key]=time.monotonic()+retry_seconds(r.headers)
            raise SourceError(platform+'_429')
        if r.status_code in (401,403): self.unavailable[key]=time.monotonic()+3600
        if r.status_code not in (200,201): raise SourceError(platform+'_http_'+str(r.status_code))
        try: return r.json()
        except ValueError: raise SourceError(platform+'_invalid_json') from None

    def confirm(self, document, positions):
        platform,account,_,_=config.identity(document)
        if platform=='direct':
            return {'verified':True,'total':float(number(document.get('sum'))/100),
                    'items':{},'source':'МойСклад: прямая продажа demand.sum'},None
        if not account: return {'error':'Не найден аккаунт по юрлицу'},None
        # Get exact marketplace SKU bridge from the real assortment or product table.
        shipped=defaultdict(lambda:Decimal(0)); mids={}
        for p in positions:
            a=p.get('assortment',{}); mid=a.get('meta',{}).get('href','').rsplit('/',1)[-1]
            sku=a.get('externalCode')
            if not sku:
                rows=db.query('SELECT DISTINCT external_code FROM products WHERE ms_id=%s AND external_code IS NOT NULL',(mid,))
                if len(rows)==1: sku=rows[0]['external_code']
            if not sku: return {'error':'Нет однозначного моста SKU для полной цены'},None
            mids[mid]=str(sku); shipped[str(sku)]+=number(p.get('quantity'))
        try:
            if platform=='yandex': raw,items,source=self.yandex(account,str(document.get('name')))
            elif platform=='ozon': raw,items,source=self.ozon(account,str(document.get('name')))
            else: raw,items,source=self.wb(account,str(document.get('name')),document)
            actual=defaultdict(lambda:{'qty':Decimal(0),'total':Decimal(0),'units':set()})
            for sku,qty,total in items:
                q=number(qty); amount=number(total)
                if q<=0 or amount<=0: raise ValueError('invalid_order_item')
                actual[str(sku)]['qty']+=q; actual[str(sku)]['total']+=amount
                actual[str(sku)]['units'].add(amount/q)
            if not set(shipped)<=set(actual) or any(actual[s]['qty']<q for s,q in shipped.items()):
                return {'error':'Состав/количество заказа не совпали с отгрузкой; полная цена не распределена','source':source},raw
            # A document may ship only part of an order. Its own quantities are
            # priced only when that SKU has one verified unit price in the order.
            totals={}
            for s,q in shipped.items():
                if q==actual[s]['qty']:
                    totals[s]=actual[s]['total']
                elif len(actual[s]['units'])==1:
                    totals[s]=next(iter(actual[s]['units']))*q
                else:
                    return {'error':'Частичная отгрузка SKU с разными ценами заказа: полная цена неоднозначна','source':source},raw
            # If distinct MS IDs share the same SKU, price cannot be split safely.
            if len(set(mids.values()))!=len(mids):
                return {'error':'Несколько товаров МС с одним SKU: полная цена неоднозначна','source':source},raw
            return {'verified':True,'total':money(sum(totals.values())),
                    'items':{m:{'total':money(totals[s])} for m,s in mids.items()},'source':source},raw
        except (SourceError,ValueError,KeyError,TypeError) as e:
            code=str(e) if isinstance(e,SourceError) else platform+'_incomplete_order_data'
            store.metric('order_source_errors',details={'platform':platform,'code':code})
            return {'error':'Не подтверждена полная цена: '+code},None

    def yandex(self, account, oid):
        if account!='ya_acc1' or not oid.isdigit(): raise SourceError('yandex_order_bridge_unknown')
        rows=db.query('SELECT payload,loaded_at FROM raw_yandex_stats_order WHERE account=%s AND order_id=%s',(account,oid))
        if rows:
            p=rows[0]['payload']
            items=[(i['shopSku'],i['count'],sum(number(x['total']) for x in i['prices'])) for i in p.get('items',[])]
            if items: return p,items,'raw_yandex_stats_order: Σ prices.total BUYER+CASHBACK+MARKETPLACE; '+str(rows[0]['loaded_at'])
        # Current business API supports exact orderIds, not a full monthly collection.
        biz=os.environ['YANDEX_BUSINESS_ID_ACC1']
        p=self.request('yandex',account,'POST',f'https://api.partner.market.yandex.ru/v1/businesses/{biz}/orders',
          headers={'Api-Key':os.environ['YANDEX_API_KEY_ACC1']},json={'orderIds':[int(oid)]},params={'limit':10})
        orders=p.get('orders',[])
        if len(orders)!=1 or str(orders[0].get('orderId'))!=oid: raise SourceError('yandex_order_not_found')
        o=orders[0]; items=[]
        for i in o.get('items',[]):
            prices=i.get('prices',{})
            # All are total item payments (count already included). Ignore delivery.
            if prices.get('payment') is None: raise SourceError('yandex_item_payment_missing')
            total=sum(number((prices.get(k) or {}).get('value',0)) for k in ('payment','subsidy','cashback'))
            items.append((i['offerId'],i['count'],total))
        return o,items,'Яндекс business orders: payment+subsidy+cashback товаров (без доставки)'

    def ozon(self, account, posting):
        if not re.fullmatch(r'\d+-\d+(?:-\d+)?',posting): raise SourceError('ozon_posting_bridge_unknown')
        # MS bridge can omit the last segment. Require real children in saved raw data.
        rows=db.query("SELECT payload,loaded_at FROM raw_ozon_posting WHERE account=%s AND (posting_number=%s OR posting_number LIKE %s) ORDER BY posting_number",(account,posting,posting+'-%' if posting.count('-')==1 else posting))
        raws=[r['payload'] for r in rows]
        if not raws:
            if posting.count('-')!=2: raise SourceError('ozon_full_posting_number_missing')
            headers={'Client-Id':os.environ['OZON_CLIENT_ID_ACC'+account[-1]],'Api-Key':os.environ['OZON_API_KEY_ACC'+account[-1]]}
            j=self.request('ozon',account,'POST','https://api-seller.ozon.ru/v3/posting/fbs/get',headers=headers,
                           json={'posting_number':posting,'with':{'financial_data':True}})
            p=j.get('result',{})
            if p.get('posting_number')!=posting: raise SourceError('ozon_posting_mismatch')
            raws=[p]
        items=[]
        for p in raws:
            for i in p.get('products',[]):
                # products.price is the seller's per-unit sale price, not customer paid after Ozon's discount.
                if i.get('currency_code','RUB')!='RUB': raise SourceError('ozon_non_rub')
                items.append((i['offer_id'],i['quantity'],number(i['price'])*number(i['quantity'])))
        return raws,items,'Ozon posting.products.price × quantity; цена продажи продавца'

    def wb(self, account, assembly, document):
        if not assembly.isdigit(): raise SourceError('wb_assembly_bridge_unknown')
        rows=db.query("""SELECT payload,loaded_at FROM raw_wb_report WHERE account=%s
            AND payload->>'assembly_id'=%s AND payload->>'supplier_oper_name'='Продажа'
            ORDER BY loaded_at DESC LIMIT 2""",(account,assembly))
        if rows:
            # Corrections/duplicate sales cannot be guessed into a price.
            vals={(str(r['payload'].get('sa_name')),str(r['payload'].get('retail_price_withdisc_rub'))) for r in rows}
            if len(vals)!=1: raise SourceError('wb_report_price_ambiguous')
            p=rows[0]['payload']
            return p,[(p['sa_name'],1,number(p['retail_price_withdisc_rub']))], 'WB отчёт: retail_price_withdisc_rub до СПП; '+str(rows[0]['loaded_at'])
        selected=os.getenv('SHIPMENT_GUARD_WB_TOKEN_ENV_ACC'+account[-1],'WB_TOKEN_ACC'+account[-1])
        if not re.fullmatch(r'WB_TOKEN_[A-Z_]*ACC'+account[-1],selected):
            raise SourceError('wb_token_reference_invalid')
        token=os.getenv('SHIPMENT_GUARD_WB_TOKEN_ACC'+account[-1]) or os.getenv(selected)
        if not token: raise SourceError('wb_marketplace_token_missing')
        stamp=int(__import__('datetime').datetime.fromisoformat(document['moment']).replace(hour=0,minute=0,second=0,microsecond=0,tzinfo=config.MS_TZ).timestamp())-14*86400
        key=(account,stamp//86400)
        cached=self.wb_cache.get(key)
        if cached and cached[0]>time.monotonic():
            orders=cached[1]
        else:
            orders=[]; cursor=0
            for _ in range(20):
                j=self.request('wb',account,'GET','https://marketplace-api.wildberries.ru/api/v3/orders',
                  headers={'Authorization':token},params={'limit':1000,'next':cursor,'dateFrom':stamp})
                page=j.get('orders',[]); orders+=page
                nxt=j.get('next')
                if not page or not nxt or nxt==cursor: break
                cursor=nxt
            else: raise SourceError('wb_order_search_bound_exceeded')
            self.wb_cache[key]=(time.monotonic()+300,orders)
        matched=[o for o in orders if str(o.get('id'))==assembly]
        if len(matched)!=1: raise SourceError('wb_order_not_found')
        o=matched[0]
        if o.get('currencyCode') in (643,'643'):
            amount=number(o['price'])/100;source='WB orders.price / 100: цена продавца до скидок WB'
        elif o.get('convertedCurrencyCode') in (643,'643'):
            amount=number(o['convertedPrice'])/100;source='WB orders.convertedPrice / 100: подтверждённая цена продавца в RUB'
        else: raise SourceError('wb_non_rub')
        return o,[(o['article'],1,amount)],source
