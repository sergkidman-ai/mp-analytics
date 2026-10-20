# поток: fin
"""TC BOM is a composition bridge only. FIFO always comes from this demand's report."""
import hashlib
import ast
import json
import os
import re
from collections import Counter,defaultdict
from decimal import Decimal
from datetime import datetime,timezone
from psycopg2.extras import Json
from core import db
from shipment_guard import store
from shipment_guard.economics import number,money
from shipment_guard.ms import SourceError


def component_counts(components):
    """Old TC caches contain stringified component records as well as plain codes."""
    out=Counter()
    entries=components.items() if isinstance(components,dict) else ((x,1) for x in components)
    for value,count in entries:
        if isinstance(value,str) and value.lstrip().startswith('{'):
            if len(value)>8192:raise SourceError('tc_bom_component_format')
            try:
                value=ast.literal_eval(value)
            except (ValueError,SyntaxError,RecursionError):
                raise SourceError('tc_bom_component_format') from None
        if isinstance(value,dict):
            if set(value)-{'external_code','incoming_references','outgoing_references'}:
                raise SourceError('tc_bom_component_format')
            value=value.get('external_code')
        if not isinstance(value,str) or not value.strip():
            raise SourceError('tc_bom_component_format')
        quantity=number(count)
        if quantity<=0:raise SourceError('tc_bom_component_quantity')
        out[value]+=quantity
    return out


class Catalogue:
    def __init__(self,request): self.request=request

    def resolve(self,sku,fetch=True):
        market_sku=sku
        # Established marketplace family code rule (wb_promo_guard._base_code).
        # Prefer exact TC codes. Never interpret an X/Х quantity marker as an alias.
        exact=db.query('SELECT 1 FROM prc_tc_model WHERE external_code=%s UNION ALL SELECT 1 FROM set_cost WHERE external_code=%s LIMIT 1',(sku,sku))
        if not exact and re.match(r'^\d{4}.+',sku) and not re.match(r'^\d{4}[XХxх]\d+',sku):
            base=sku[:4]
            if db.query('SELECT 1 FROM prc_tc_model WHERE external_code=%s UNION ALL SELECT 1 FROM set_cost WHERE external_code=%s LIMIT 1',(base,base)):
                sku=base
        bom,source=self._resolve_exact(sku,fetch)
        bom,nested=self._flatten(bom,{sku})
        if nested:source=dict(source,nested_compositions=nested)
        if market_sku!=sku:
            source=dict(source,market_sku=market_sku,tc_external_code=sku,
                code_bridge='Принятое правило кода семейства: первые 4 символа; состав/количество только из ТК')
        return bom,source

    def _flatten(self,bom,path):
        if len(path)>10:raise SourceError('tc_bom_depth_limit')
        rows=db.query('SELECT external_code,components,resolved_at FROM set_cost WHERE external_code=ANY(%s)',(list(bom),))
        nested={r['external_code']:r for r in rows if r['components']}
        out=Counter();proof={}
        for component,n in bom.items():
            if component not in nested:out[component]+=n;continue
            if component in path:raise SourceError('tc_bom_cycle')
            row=nested[component];flat,children=self._flatten(component_counts(row['components']),path|{component})
            for c,count in flat.items():out[c]+=n*count
            proof[component]={'components':row['components'],'resolved_at':str(row['resolved_at']),'nested':children}
        return out,proof

    def _resolve_exact(self,sku,fetch=True):
        rows=db.query('SELECT components,resolved_at FROM set_cost WHERE external_code=%s',(sku,))
        if rows and rows[0].get('components'):
            components=rows[0]['components']
            return component_counts(components),{'source':'ТК set_cost.components','is_set':True,'resolved_at':str(rows[0]['resolved_at']),
                 'sha256':hashlib.sha256(json.dumps(components,ensure_ascii=False).encode()).hexdigest()}
        rows=db.query('SELECT * FROM ms_shipment_bom WHERE external_code=%s AND valid_until>now()',(sku,))
        if rows:
            r=rows[0]
            return component_counts(r['components']) if r['kind']=='set' else Counter({sku:1}),{
                'source':r['source'],'is_set':r['kind']=='set','resolved_at':str(r['resolved_at']),'sha256':r['source_sha256'],'lookup_error':r['raw_payload'].get('error') if isinstance(r['raw_payload'],dict) else None}
        if not fetch:return Counter({sku:1}),{'source':'Состав отсутствует в локальном справочнике ТК'}
        key=os.getenv('CARTRIDGE_API_KEY')
        if not key:raise SourceError('tc_api_key_missing')
        payload=self.request('tc','catalog','POST',os.getenv('CARTRIDGE_API_URL','https://thecartridge.ru/api/catalog/mix_data'),
             headers={'Api-Key':key,'Content-Type':'application/json'},json={'external_code':sku})
        if isinstance(payload,list) and payload and len(payload)<=1000 and all(isinstance(v,(str,int)) and str(v) for v in payload):
            comps=[str(v) for v in payload];kind='set'
        elif isinstance(payload,dict) and payload.get('error') in ('not_mix','external_code_not_found'):
            comps=[];kind='not_mix'
        else:raise SourceError('tc_composition_incomplete')
        digest=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with store.transaction() as c:
            c.execute("""INSERT INTO ms_shipment_bom(external_code,components,kind,source,source_sha256,raw_payload,valid_until)
              VALUES(%s,%s,%s,'ТК API mix_data',%s,%s,now()+interval '1 day')
              ON CONFLICT(external_code) DO UPDATE SET components=excluded.components,kind=excluded.kind,
              source=excluded.source,source_sha256=excluded.source_sha256,raw_payload=excluded.raw_payload,
              resolved_at=now(),valid_until=excluded.valid_until""",(sku,Json(comps),kind,digest,Json(payload)))
        return Counter(comps) if comps else Counter({sku:1}),{
             'source':'ТК API mix_data','is_set':kind=='set','resolved_at':datetime.now(timezone.utc).isoformat(),'sha256':digest,'lookup_error':payload.get('error') if isinstance(payload,dict) else None}


def match(shipped,mid_skus,items,boms=None,sources=None):
    """Verify physical quantities against BOM; never split FIFO between overlapping sale SKUs."""
    actual=defaultdict(lambda:{'qty':Decimal(0),'total':Decimal(0),'units':set()})
    for sku,qty,total in items:
        q=number(qty);amount=number(total)
        if q<=0 or amount<=0:raise ValueError('invalid_order_item')
        actual[str(sku)]['qty']+=q;actual[str(sku)]['total']+=amount
        actual[str(sku)]['units'].add(amount/q)
    if not actual:return {'error':'Нет товарных позиций заказа','match_error':'mismatch'}
    compositions={sku:Counter((boms or {}).get(sku,{sku:1})) for sku in actual}
    expanded=Counter()
    for sku,a in actual.items():
        for component,n in compositions[sku].items():
            if n<=0:raise ValueError('invalid_bom')
            expanded[component]+=a['qty']*n
    selected={}
    if dict(expanded)==dict(shipped):
        selected={s:a['qty'] for s,a in actual.items()}
    else:
        # Partial shipment is safe when each physical component has one sale owner.
        owners=Counter(c for bom in compositions.values() for c in bom)
        relevant={s:bom for s,bom in compositions.items() if set(bom)&set(shipped)}
        if any(owners[c]>1 for c in shipped):
            return {'error':'Частичная отгрузка с пересекающимися составами наборов: состав неоднозначен','match_error':'mismatch'}
        for s,bom in relevant.items():
            ratios={number(shipped.get(c,0))/n for c,n in bom.items()}
            if len(ratios)!=1: return mismatch(shipped,expanded,sources)
            q=ratios.pop()
            if q<=0 or q>actual[s]['qty'] or q!=q.to_integral_value():return mismatch(shipped,expanded,sources)
            selected[s]=q
        used=Counter()
        for s,q in selected.items():
            for c,n in compositions[s].items():used[c]+=q*n
        if dict(used)!=dict(shipped):return mismatch(shipped,expanded,sources)
    totals={}
    for s,q in selected.items():
        if q==actual[s]['qty']:totals[s]=actual[s]['total']
        elif len(actual[s]['units'])==1:totals[s]=next(iter(actual[s]['units']))*q
        else:return {'error':'Частичная отгрузка SKU с разными ценами заказа: полная цена неоднозначна','match_error':'price_ambiguous'}
    # Connected components keep overlapping kits together; no proportional allocation of actual FIFO.
    clusters=[]
    for sku in selected:
        component_set=set(compositions[sku]);joined={sku};rest=[]
        for cluster,components in clusters:
            if components&component_set:joined|=cluster;component_set|=components
            else:rest.append((cluster,components))
        # A join can connect formerly disjoint clusters encountered before the join.
        changed=True
        while changed:
            changed=False;again=[]
            for cluster,components in rest:
                if components&component_set:joined|=cluster;component_set|=components;changed=True
                else:again.append((cluster,components))
            rest=again
        clusters=rest+[(joined,component_set)]
    groups=[]
    for sale_skus,components in clusters:
        mids=sorted(m for m,s in mid_skus.items() if s in components)
        groups.append({'sku':' + '.join(sorted(sale_skus)),'sale_skus':sorted(sale_skus),
          'quantity':float(sum(selected[s] for s in sale_skus)),'total':money(sum(totals[s] for s in sale_skus)),
          'ms_ids':mids,'ambiguous_fifo':len(sale_skus)>1,
          'composition':[{ 'sku':c,'quantity':float(shipped[c])} for c in sorted(components)],
          'bom_sources':{s:(sources or {}).get(s,{'source':'Точный внешний код одиночного товара'}) for s in sorted(sale_skus)}})
    legacy_items={g['ms_ids'][0]:{'total':g['total']} for g in groups if len(g['ms_ids'])==1 and not g['ambiguous_fifo']}
    return {'verified':True,'total':money(sum(totals.values())),'items':legacy_items,'groups':groups}


def mismatch(shipped,expanded,sources=None):
    missing={s:float(q-expanded.get(s,0)) for s,q in shipped.items() if q>expanded.get(s,0)}
    return {'error':'Состав/количество заказа не совпали с отгрузкой после проверки справочника ТК',
       'match_error':'mismatch','composition_diagnostic':{'ms_quantity':{s:float(q) for s,q in shipped.items()},
          'order_components_quantity':{s:float(q) for s,q in expanded.items()},'unmatched_quantity':missing,'bom_sources':sources or {}}}
