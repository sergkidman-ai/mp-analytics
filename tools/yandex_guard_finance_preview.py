# поток: fin
"""Read-only promotion snapshot and exact old/new finance comparison; never calls push/save."""
import argparse
from collections import Counter
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import hashlib
from pathlib import Path
from collectors.yandex_finance_basis import load,save,stamp,request,lock
from ops import ya_promo_guard as guard


def fetch(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    biz=guard._biz(); acts=[];raw={};rows=[];token=None;seen=set()
    while True:
        result=request('POST',f'/v2/businesses/{biz}/promos',params={'page_token':token} if token else {},
                       json={'participation':'PARTICIPATING_NOW'})
        if 'promos' not in result:raise RuntimeError('Missing promos collection')
        acts.extend(result['promos']);token=(result.get('paging') or {}).get('nextPageToken')
        if not token:break
        if token in seen or len(seen)>=200:raise RuntimeError('Incomplete promos pagination')
        seen.add(token)
    for a in acts:
        token=None;seen=set();pages=0;offers=[]
        while True:
            result=request('POST',f'/v2/businesses/{biz}/promos/offers',
                           params=dict(limit=500,**({'page_token':token} if token else {})),json={'promoId':a['id']})
            if 'offers' not in result: raise RuntimeError('Missing promo offers collection')
            offers.extend(result['offers']);pages+=1
            token=(result.get('paging') or {}).get('nextPageToken')
            if not token:break
            if token in seen or pages>=200:raise RuntimeError('Incomplete promo pagination')
            seen.add(token)
        raw[str(a['id'])]={'offers':offers,'pages':pages,'complete':True}
        mi=a.get('mechanicsInfo') or {};mech=[m.get('type') for m in mi] if isinstance(mi,list) else [mi.get('type')]
        for o in offers:
            d=(o.get('params') or {}).get('discountParams') or o.get('discountParams') or {}
            rows.append({'offer_id':o.get('offerId'),'promo_id':a['id'],'promo_name':a.get('name'),
                         'mechanics':','.join(m for m in mech if m),'status':o.get('status'),
                         'price':d.get('price'),'promo_price':d.get('promoPrice'),
                         'max_promo_price':d.get('maxPromoPrice'),'levels':d.get('bestPriceLevels') or {}})
    save(folder/'promos_raw.json',{'received_at':stamp(),'promos':acts,'offers':raw})
    enrich_catalog(folder, rows)
    cmap=guard.g.cost_map(sorted({r['offer_id'] for r in rows if r['offer_id']}))
    for r in rows:r['cogs'],r['cogs_source']=cmap.get(r['offer_id'],(None,'НЕТ'))
    save(folder/'snapshot.json',{'received_at':stamp(),'rows':rows})
    return rows


def enrich_catalog(folder, rows):
    ids=sorted({r['offer_id'] for r in rows if r['offer_id']});prices={};raw=[]
    for k in range(0,len(ids),200):
        result=request('POST',f'/v2/businesses/{guard._biz()}/offer-prices',params={'limit':200},json={'offerIds':ids[k:k+200]})
        if 'offers' not in result or (result.get('paging') or {}).get('nextPageToken'):
            raise RuntimeError('Incomplete catalog price response')
        raw.append(result)
        for o in result['offers']:
            v=(o.get('price') or {}).get('value')
            if v is not None: prices[o['offerId']]=v
    save(Path(folder)/'catalog_raw.json',{'received_at':stamp(),'batches':raw})
    for r in rows:
        r['catalog_price']=prices.get(r['offer_id'])
        if not r.get('price'):r['price']=r['catalog_price']


def classify(r,keep):
    out={'floor':None,'target':None,'action':'none'}
    if r['offer_id']=='7200':return dict(out,category='excluded_7200')
    if r['status'] in guard.LIVE_STATUSES and not r.get('promo_price'):
        return dict(out,category='no_data',reason='API не возвращает явную промо-цену действующего участия')
    if r.get('cogs') is None or D(str(r['cogs']))<=0 or keep<=0:
        return dict(out,category='no_data',reason='Себестоимость/доля продавца не подтверждены')
    cost=D(str(r['cogs']));floor=(cost+max(D(300),cost/D(10)))/keep
    target=floor.to_integral_value(rounding=ROUND_CEILING)
    cap=min(D(str(r.get('max_promo_price') or 0)),(D(str(r.get('price') or 0))*D('.95')).to_integral_value(rounding=ROUND_FLOOR))
    out.update(floor=floor,target=target,cap=cap)
    active=r['status'] in guard.LIVE_STATUSES
    editable=any(m in guard.EDITABLE for m in r['mechanics'].split(',')) and not any(m in guard.SKIP_MECHANICS for m in r['mechanics'].split(','))
    if any(m in guard.SKIP_MECHANICS for m in r['mechanics'].split(',')):
        return dict(out,category='mechanics_unavailable')
    if active and D(str(r.get('promo_price') or 0))>0 and D(str(r['promo_price']))>=floor-D('.5'):
        return dict(out,category='passes_floor')
    if not r.get('price'):
        return dict(out,category='no_data',reason='Цена каталога отсутствует')
    if r['status'] not in guard.LIVE_STATUSES and r['status'] not in ('NOT_PARTICIPATING','RENEW_FAILED'):
        return dict(out,category='status_unavailable')
    if not editable or cap<=0 or target>cap:
        return dict(out,category='cap_or_mechanics_unavailable',target=None,action='remove' if active else 'none')
    if not active:
        lo = D(str(r['price'])) * D('.01')
        for key in ('topBestLevel','superBestLevel','bestLevel','lightBestLevel'):
            level=D(str((r.get('levels') or {}).get(key) or 0))
            if level>0 and level>=floor-D('.5') and lo<=level<=cap:
                return dict(out,category='eligible_entry',action='add',target=level,level=key)
        return dict(out,category='levels_unavailable',action='none')
    return dict(out,category='below_floor',action='raise')


def compare(folder,basis_path,baseline_path):
    folder=Path(folder);b=load(basis_path)
    if not b.get('price_usable'):raise RuntimeError('Candidate is not verified')
    hist=load(baseline_path);i=hist['period_keys'].index(b['month']);L=hist['accounts']['ya_acc1']['lines']
    from collectors.yandex_finance_basis import EXPENSES
    old_ret=sum((D(str(L[k][i])) for k in EXPENSES),D(0))/(D(str(L['revenue'][i]))+D(str(L['netting'][i])))
    rows=load(folder/'snapshot.json')['rows'];out=[]
    for r in rows:
        before=classify(r,1-old_ret);after=classify(r,D(str(b['keep'])))
        category=after['category']
        if after['category']=='passes_floor':
            category='unchanged' if before['category']=='passes_floor' else 'passes_new_floor'
        changed = before['action'] != after['action'] or (before['action'] in ('add','raise') and before['target'] != after['target'])
        out.append(dict(r,old=before,new=after,category=category,changed_by_basis=changed,basis_version=b['version']))
    proposals=[r for r in out if r['new']['action']!='none']
    summary={'received_at':load(folder/'snapshot.json')['received_at'],'basis_version':b['version'],
             'old_retention':old_ret,'new_retention':b['retention'],'rows':len(out),
             'unique_skus':len({r['offer_id'] for r in out}), 'categories':dict(Counter(r['category'] for r in out)),
             'basis_changed_actions':dict(Counter(r['new']['action'] for r in proposals if r['changed_by_basis'])),
             'proposed_actions':dict(Counter(r['new']['action'] for r in proposals)),
             'sku_checks':{sku:[r for r in out if r['offer_id']==sku] for sku in ['34749','347410','22262','7200']}}
    with lock(folder/'.compare.lock'):
        save(folder/'comparison.json',out);save(folder/'proposed_actions.json',proposals);save(folder/'basis_changed_actions.json',[r for r in proposals if r['changed_by_basis']]);save(folder/'summary.json',summary)
    # CSV is the reviewable complete per-SKU list, including reasons and missing data.
    import csv
    with (folder/'comparison.csv').open('w') as f:
        cols=['offer_id','promo_id','status','mechanics','cogs','cogs_source','price','promo_price','max_promo_price','old_floor','new_floor','cap','target','category','action','changed_by_basis','reason']
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for r in out:w.writerow({**{k:r.get(k) for k in cols[:9]},'old_floor':r['old']['floor'],'new_floor':r['new']['floor'],
                                  'cap':r['new'].get('cap'),'target':r['new']['target'],'category':r['category'],'action':r['new']['action'],'changed_by_basis':r['changed_by_basis'],'reason':r['new'].get('reason',r['new']['category'])})
    return summary


def main():
    a=argparse.ArgumentParser();a.add_argument('--output',required=True);a.add_argument('--basis');a.add_argument('--baseline');a.add_argument('--reuse-snapshot',action='store_true');x=a.parse_args()
    if not x.reuse_snapshot:fetch(x.output)
    if x.basis:
        s=compare(x.output,x.basis,x.baseline)
        print({k:s[k] for k in ['rows','unique_skus','categories','proposed_actions']})
if __name__=='__main__':main()
