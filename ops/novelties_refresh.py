# поток: prc
"""Read marketplace/MS catalogues and update local novelty monitoring only.
No MS/MP mutations, financial rebuilds, paid research or notifications.
"""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import time
import requests
from core import db

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'.private/novelties_refresh'


def refresh_ms():
    from collectors.moysklad import iter_product_pages,load_raw
    n=0
    for page in iter_product_pages():
        load_raw(page)
        rows=[dict(ms_id=p['id'],name=p.get('name'),article=p.get('article'),code=p.get('code'),
                   external_code=p.get('externalCode'),archived=bool(p.get('archived'))) for p in page]
        db.upsert('ms_product',rows,['ms_id'],update_cols=['name','article','code','external_code','archived'])
        n+=len(rows)
    return n


def market_offers(session=None,pause=time.sleep):
    from collectors.yandex_monthly import API,_offer_rows,_save_offers
    client=session or requests.Session()
    url=f'{API}/v2/businesses/{os.environ["YANDEX_BUSINESS_ID_ACC1"]}/offer-mappings'
    headers={'Api-Key':os.environ['YANDEX_API_KEY_ACC1']}
    token=None;seen=set();n=0
    for _ in range(2000):
        if token in seen:raise ValueError('market_repeated_page_token')
        seen.add(token);params={'limit':100}
        if token:params['pageToken']=token
        for attempt in range(4):
            try:
                r=client.post(url,headers=headers,params=params,json={},timeout=40)
                if r.status_code in (420,429,500,502,503,504):pause(min(30,5*(attempt+1)));continue
                r.raise_for_status();data=r.json().get('result');break
            except (requests.Timeout,requests.ConnectionError):
                if attempt==3:raise
                pause(3*(attempt+1))
        else:raise RuntimeError('market_source_unavailable')
        if not isinstance(data,dict) or not isinstance(data.get('offerMappings'),list):raise ValueError('market_incomplete_response')
        items=data['offerMappings'];nxt=data.get('paging',{}).get('nextPageToken')
        if nxt and not items:raise ValueError('market_empty_nonfinal_page')
        n+=_save_offers(_offer_rows(items))
        if not nxt:return n
        token=nxt;pause(.7)
    raise ValueError('market_pagination_cap')


def refresh_unlinked():
    from prices import unlinked
    rows=unlinked.collect();hits=unlinked.suggest(rows);unlinked.save(rows,hits)
    return {'rows':len(rows),'with_candidates':sum(bool(hits.get(r['ms_id'])) for r in rows)}


def refresh():
    from collectors.ozon_products import main as ozon
    from tools.card_wb_presence import check as wb
    from collectors.launch_track import main as track
    RUNTIME.mkdir(exist_ok=True)
    results={}
    def run(name,fn):
        try:
            value=fn()
            if name.startswith('wb_') and value is None:raise ValueError('wb_empty_catalogue')
            return dict(status='ok',result=value,completed_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:return dict(status='error',error_type=type(e).__name__,completed_at=datetime.now(timezone.utc).isoformat())
    with (RUNTIME/'lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'busy':True}
        results['ms']=run('ms',refresh_ms)
        steps={'oz_acc1':lambda:ozon('oz_acc1'),'oz_acc2':lambda:ozon('oz_acc2'),
               'wb_acc1':lambda:wb('wb_acc1'),'wb_acc2':lambda:wb('wb_acc2'),'ya_acc1':market_offers}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures={pool.submit(run,k,v):k for k,v in steps.items()}
            for future in as_completed(futures):
                name=futures[future];results[name]=future.result()
                print(name,results[name]['status'],flush=True)
        failed={k for k in steps if results[k]['status']!='ok'}
        if results['ms']['status']=='ok':results['tracker']=run('tracker',lambda:track(failed_sources=failed))
        else:results['tracker']={'status':'error','error_type':'MS_catalogue_unavailable'}
        results['unlinked']=run('unlinked',refresh_unlinked)
        report={'at':datetime.now(timezone.utc).isoformat(),'steps':results}
        temp=RUNTIME/'latest.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,default=str));temp.replace(RUNTIME/'latest.json')
        return report


if __name__=='__main__':
    result=refresh();print(json.dumps(result,ensure_ascii=False,default=str))
    if any(v['status']!='ok' for v in result.get('steps',{}).values()):raise SystemExit(1)
