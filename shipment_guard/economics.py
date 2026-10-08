# поток: fin
"""Real document quantities and total FIFO. No catalogue costs or imputation."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime
from shipment_guard import config
D=Decimal

def number(value):
    if value is None or isinstance(value,bool): raise ValueError('missing_number')
    try: n=D(str(value))
    except (InvalidOperation,ValueError): raise ValueError('invalid_number') from None
    if not n.is_finite(): raise ValueError('invalid_number')
    return n

def money(v): return float(v.quantize(D('.01')))

def ms_time(value):
    if not value: return None
    return datetime.fromisoformat(value).replace(tzinfo=config.MS_TZ).isoformat()

def assess(document, positions, fifo, confirmed, rule):
    platform,account,organization,_=config.identity(document)
    result={'name':document.get('name'),'updated':document.get('updated'),
       'moment':ms_time(document.get('moment')),'platform':platform,'account':account,
       'organization':organization,'order_id':document.get('name'),
       'document_price':None,'full_price':None,'fifo':None,'floor':None,'deviation':None,
       'signals':[],'errors':[],'lines':[],
       'sources':{'document':'МойСклад demand.sum (копейки)','fifo':'report/stock/byoperation.cost (итог, копейки)',
          'quantity':'demand.positions.quantity','full_price':confirmed.get('source'),
          'expenses':'Предварительная база расходов; не фактическая комиссия заказа'},
       'rule':None,'status':'ok'}
    errors=result['errors']
    try:
        document_price=number(document.get('sum'))/100
        if document_price<=0: raise ValueError()
        result['document_price']=money(document_price)
    except ValueError: errors.append('Цена документа отсутствует или неположительна')
    grouped=defaultdict(list)
    for pos in positions:
        try:
            meta=pos.get('assortment',{}).get('meta',{})
            mid=meta.get('href','').rsplit('/',1)[-1]
            qty=number(pos.get('quantity')); price=number(pos.get('price'))/100
            discount=number(pos.get('discount',0))
            if not mid or qty<=0 or price<=0 or not 0<=discount<=100: raise ValueError()
            unit=price*(1-discount/100)
            # VAT-exclusive prices need their actual VAT to match document.sum.
            if document.get('vatEnabled') and not document.get('vatIncluded'):
                if pos.get('vatEnabled') is False: vat=D(0)
                else: vat=number(pos.get('vat'))
                unit*=1+vat/100
            grouped[mid].append({'qty':qty,'unit':unit,'price':unit*qty,
              'sku':pos.get('assortment',{}).get('externalCode') or pos.get('assortment',{}).get('code') or mid})
        except ValueError: errors.append('Неполная цена/количество/НДС позиции')
    if not grouped: errors.append('Нет проверяемых товарных позиций')
    costs=defaultdict(lambda:D(0))
    for row in fifo:
        for p in row.get('positions',[]):
            mid=p.get('meta',{}).get('href','').rsplit('/',1)[-1]
            try: costs[mid]+=number(p.get('cost'))/100
            except ValueError: errors.append('Неполный FIFO отчёта')
    unexpected=set(costs)-set(grouped)
    if unexpected: errors.append('FIFO содержит товар вне позиций отгрузки')
    complete=bool(grouped) and all(costs[mid]>0 for mid in grouped) and not unexpected
    if not complete: errors.append('FIFO отсутствует, нулевой или неполный; без подстановки')
    if complete: result['fifo']=money(sum(costs[mid] for mid in grouped))
    line_sum=sum((x['price'] for group in grouped.values() for x in group),D(0))
    if result['document_price'] is not None and abs(line_sum-D(str(result['document_price'])))>D('.02'):
        errors.append('Сумма позиций с учётом скидок/НДС не совпала с demand.sum')
    full=confirmed.get('total'); full_items=confirmed.get('items',{})
    # Adapter must verify exactly the shipped quantities, not merely order.total.
    full_verified=confirmed.get('verified',False)
    try:
        full=number(full)
        if not full_verified or full<=0: raise ValueError()
        result['full_price']=money(full)
        result['price_difference']=money(full-D(str(result['document_price']))) if result['document_price'] is not None else None
        result['price_difference_reason']='Полная база площадки отличается от документа: сохранены обе суммы' if result.get('price_difference') else None
    except ValueError:
        full=None; errors.append(confirmed.get('error') or 'Полная цена площадки не подтверждена')
    valid_rule=False
    if rule:
        try:
            retention=number(rule['retention'])
            valid_rule=0<=retention<1 and rule['valid_until']>=datetime.now(config.MS_TZ).date() and rule['platform']==platform
            if valid_rule:
                result['rule']={k:str(rule[k]) for k in ['version','retention','base_from','base_to','source','source_sha256']}
        except (ValueError,KeyError): pass
    if not valid_rule: result['signals'].append('rule_unknown')
    sale_groups=confirmed.get('groups') if full_verified else None
    if sale_groups:
        assigned=[m for g in sale_groups for m in g['ms_ids']]
        if set(assigned)!=set(grouped) or len(assigned)!=len(set(assigned)):
            errors.append('Состав проверки не покрывает позиции МС однозначно')
            sale_groups=None
    if not sale_groups:
        sale_groups=[]
        for mid,group in grouped.items():
            item=full_items.get(mid)
            price=number(item['total']) if item and full_verified else sum(x['price'] for x in group) if platform=='direct' and full_verified else None
            sale_groups.append({'sku':group[0]['sku'],'quantity':float(sum(x['qty'] for x in group)),
              'ms_ids':[mid],'total':money(price) if price is not None else None,
              'ambiguous_fifo':False,'composition':[],'bom_sources':{},
              'different_position_prices':len({x['unit'] for x in group})>1})
    for g in sale_groups:
        mids=g['ms_ids'];qty=number(g['quantity'])
        doc_price=sum(x['price'] for mid in mids for x in grouped[mid])
        item_price=number(g['total']) if g.get('total') is not None and full_verified else None
        cost=sum(costs[mid] for mid in mids) if all(costs[mid]>0 for mid in mids) else None
        ambiguous=g.get('ambiguous_fifo',False);signals=[];floor=None
        if item_price is not None and cost is not None and item_price<cost:signals.append('below_fifo')
        if valid_rule and cost is not None and not ambiguous:
            # Absolute promo target per SOLD SKU/unit (one kit, not its physical components).
            floor=(cost+max(D(300)*qty,D('.1')*cost))/(1-retention)
            if item_price is not None and item_price<floor:signals.append('below_floor')
        if ambiguous:
            errors.append('Наборы '+g['sku']+' имеют общие компоненты: FIFO проверен по группе, по наборам не распределён')
        has_bom=bool(g.get('bom_sources')) and any(s.get('is_set',s.get('source','').endswith('set_cost.components')) for s in g['bom_sources'].values())
        level='Группа с общими компонентами; FIFO по отдельным наборам не распределён' if ambiguous else 'Набор по составу ТК' if has_bom else 'Товар целиком; FIFO по строкам с разной ценой не распределён' if g.get('different_position_prices') else 'Товар'
        components=[{'ms_id':mid,'sku':grouped[mid][0]['sku'],
          'quantity':float(sum(x['qty'] for x in grouped[mid])),
          'fifo':money(costs[mid]) if costs[mid]>0 else None,
          'document_price':money(sum(x['price'] for x in grouped[mid]))} for mid in mids]
        result['lines'].append({'ms_id':mids[0] if len(mids)==1 else None,'ms_ids':mids,'sku':g['sku'],
          'quantity':float(qty),'document_price':money(doc_price),
          'full_price':money(item_price) if item_price is not None else None,
          'fifo':money(cost) if cost is not None else None,'floor':money(floor) if floor is not None else None,
          'deviation':money(item_price-floor) if item_price is not None and floor is not None else None,
          'level':level,'signals':signals,'position_count':sum(len(grouped[mid]) for mid in mids),
          'components':components,'bom_sources':g.get('bom_sources',{})})
        result['signals']+=signals
    if confirmed.get('composition_diagnostic'):
        result['composition_diagnostic']=confirmed['composition_diagnostic']
    if any(g.get('bom_sources') for g in sale_groups):
        result['sources']['composition']='Справочник ТК: внешний код набора → внешние коды/количество компонентов; стоимость справочника не используется'
    if complete and full is not None and full<sum(costs[mid] for mid in grouped): result['signals'].append('below_fifo')
    floors=[l['floor'] for l in result['lines']]
    if valid_rule and floors and all(f is not None for f in floors):
        result['floor']=round(sum(floors),2)
        if full is not None:
            result['deviation']=money(full-D(str(result['floor'])))
            if full<D(str(result['floor'])): result['signals'].append('below_floor')
    if errors:
        result['signals'].append('data_error'); result['status']='data_error'
    elif 'below_fifo' in result['signals']: result['status']='below_fifo'
    elif 'below_floor' in result['signals']: result['status']='below_floor'
    elif not valid_rule: result['status']='rule_unknown'
    result['signals']=sorted(set(result['signals']))
    return result
