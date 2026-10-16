# поток: ev
"""Latest eligible MS shipments, one shipment per lost WB unit, no reuse within account.
Actual FIFO cost only. Missing cost never causes selection of an older shipment.
"""
import argparse
from collections import Counter, deque
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import time
from psycopg2.extras import Json
from core import db


def allocate(items):
    """Maximum coverage, then maximum sum of shipment timestamps (latest first).
    Stable warehouse/nm ordering resolves indistinguishable warehouse allocations.
    Successive shortest residual paths permit reassignment instead of greedy undercoverage.
    """
    items=sorted(items,key=lambda i:(i['warehouse'],i['vendor_code'],i['nm_id'],i['document_id']))
    demands={e['demand_id']:e for i in items for e in i['evidence'] if e['usable_candidate']}
    ids=sorted(demands,key=lambda k:(demands[k]['moment'],k),reverse=True)
    source=0;row_start=1;doc_start=1+len(items);sink=doc_start+len(ids)
    graph=[[] for _ in range(sink+1)];refs={};nodes={k:doc_start+n for n,k in enumerate(ids)}
    def edge(a,b,capacity,cost):
        forward=[b,len(graph[b]),capacity,cost]
        reverse=[a,len(graph[a]),0,-cost]
        graph[a].append(forward);graph[b].append(reverse)
        return forward
    for n,i in enumerate(items):
        edge(source,row_start+n,i['qty'],0)
        eligible={e['demand_id'] for e in i['evidence'] if e['usable_candidate']}
        refs[n]=[(k,edge(row_start+n,nodes[k],1,0)) for k in ids if k in eligible]
    for k in ids:edge(nodes[k],sink,1,-int(datetime.fromisoformat(demands[k]['moment']).timestamp()))
    flow=0
    while True:
        dist=[float('inf')]*len(graph);dist[source]=0;prev=[None]*len(graph)
        queue=deque([source]);inside={source}
        while queue:
            a=queue.popleft();inside.remove(a)
            for index,e in enumerate(graph[a]):
                b,_,cap,cost=e
                if cap and dist[b]>dist[a]+cost:
                    dist[b]=dist[a]+cost;prev[b]=(a,index)
                    if b not in inside:queue.append(b);inside.add(b)
        if prev[sink] is None:break
        b=sink
        while b!=source:
            a,index=prev[b];e=graph[a][index];e[2]-=1;graph[b][e[1]][2]+=1;b=a
        flow+=1
    selected=[]
    for n,i in enumerate(items):
        chosen=[demands[k] for k,e in refs[n] if e[2]==0]
        chosen.sort(key=lambda e:(e['moment'],e['demand_id']),reverse=True)
        selected.append(dict(document_id=i['document_id'],warehouse=i['warehouse'],nm_id=i['nm_id'],
            vendor_code=i['vendor_code'],qty=i['qty'],selected=chosen,unmatched_qty=i['qty']-len(chosen)))
    assert sum(len(i['selected']) for i in selected)==flow
    assert len({e['demand_id'] for i in selected for e in i['selected']})==flow
    return selected


def raw_positions(raw):
    out=Counter()
    for p in raw['positions']['rows']:
        key=p['assortment']['meta']['href'].split('/')[-1]
        out[key]+=Decimal(str(p['quantity']))
    return out


def fetch_costs(account,selected):
    from core import ms_api as ms
    demands={e['demand_id']:e for i in selected for e in i['selected']}
    stored={r['demand_id']:r for r in db.query('SELECT * FROM wb_loss_demand_cost WHERE account=%s',(account,))}
    snapshots={d['id']:d for r in db.query('SELECT payload FROM wb_loss_ms_lookup WHERE account=%s',(account,)) for d in r['payload']['rows']}
    cached=db.query('''SELECT d.demand_id,d.cogs,d.loaded_at,p.ms_id,p.qty,p.cost FROM ms_demand_cogs d
        JOIN wb_cogs_demand w ON w.demand_id=d.demand_id AND w.account=%s
        JOIN ms_demand_pos p ON p.demand_id=d.demand_id WHERE d.demand_id=ANY(%s)''',(account,list(demands)))
    by_id={}
    # A demand can have more than one financial summary row: deduplicate position ids.
    for r in cached:by_id.setdefault(r['demand_id'],{})[r['ms_id']]=r
    for number,(did,e) in enumerate(demands.items(),1):
        if did in stored:continue
        positions=by_id.get(did,{})
        cached_qty=Counter({k:Decimal(str(p['qty'])) for k,p in positions.items()})
        expected=raw_positions(snapshots[did]) if did in snapshots else cached_qty
        evidence_qty=sum(Decimal(p['qty']) for p in e['positions'])
        cache_ok=bool(positions) and expected==cached_qty and sum(expected.values())==evidence_qty
        cache_ok=cache_ok and all(p['cost'] is not None and p['cost']>0 and p['qty']>0 for p in positions.values())
        if cache_ok:
            total=sum(Decimal(str(p['cost'])) for p in positions.values())
            cache_ok=all(abs(total-Decimal(str(p['cogs'])))<Decimal('0.01') for p in positions.values())
        if cache_ok:
            payload=dict(source='ms_demand_pos / MS report/stock/byoperation',
                source_loaded_at=str(max(p['loaded_at'] for p in positions.values())),
                positions=[dict(ms_id=k,qty=str(p['qty']),cost_rub=str(p['cost'])) for k,p in positions.items()])
            state='ok'
        else:
            # Fresh raw source for absent/incomplete/discordant financial cache.
            if did not in snapshots:
                raw=ms.get('/entity/demand/'+did,{'expand':'positions.assortment'})
                if raw['positions']['meta']['size']!=len(raw['positions'].get('rows',[])):
                    raise ValueError('Incomplete live MS positions')
                snapshots[did]=raw
            expected=raw_positions(snapshots[did])
            report=ms.get('/report/stock/byoperation',{'operation.id':did})
            payload=dict(source='MS report/stock/byoperation',report=report,
                actual_positions={k:str(v) for k,v in expected.items()},demand=snapshots[did])
            rows=report.get('rows',[]);parts=rows[0].get('positions',[]) if len(rows)==1 else []
            actual={p.get('meta',{}).get('href','').split('/')[-1]:p for p in parts}
            state='ok'
            if not expected or set(expected)!=set(actual) or len(actual)!=len(parts):state='incomplete_cost'
            if any(q<=0 for q in expected.values()):state='invalid_quantity'
            if sum(expected.values())!=evidence_qty:state='positions_changed'
            if not snapshots[did].get('applicable'):state='not_applied'
            if any(p.get('cost') is None or Decimal(str(p['cost']))<=0 for p in parts):state='missing_cost'
            total=sum(Decimal(str(p['cost']))/100 for p in parts) if state=='ok' else None
            time.sleep(0.2)
        total=total.quantize(Decimal('0.01')) if total is not None else None
        db.upsert('wb_loss_demand_cost',[dict(account=account,demand_id=did,payload=Json(payload),
            total=total,state=state)],['account','demand_id'])
        if number%25==0 or number==len(demands):print(account,'cost documents',number,'/',len(demands),flush=True)
    return {r['demand_id']:r for r in db.query('SELECT demand_id,total,state,payload->>\'source\' source FROM wb_loss_demand_cost WHERE account=%s',(account,))}


def calculate(account):
    rows=db.query('SELECT payload,built_at FROM wb_loss_analysis WHERE account=%s',(account,))
    if not rows:raise ValueError('Candidate analysis required')
    analysis=rows[0]['payload'];selected=allocate(analysis['items'])
    costs=fetch_costs(account,selected);grand=Decimal(0);priced=0;allocated=0
    for i in selected:
        subtotal=Decimal(0);missing=0
        for e in i['selected']:
            c=costs[e['demand_id']];e['cost_state']=c['state'];e['cost_source']=c['source']
            e['cost_rub']=str(c['total']) if c['state']=='ok' and c['total'] is not None else None
            if e['cost_rub'] is not None:subtotal+=Decimal(e['cost_rub'])
            else:missing+=1
        i['costed_qty']=len(i['selected'])-missing;i['missing_cost_qty']=missing
        i['known_cost_rub']=str(subtotal);i['complete']=not (missing or i['unmatched_qty'])
        i['total_cost_rub']=str(subtotal) if i['complete'] else None
        grand+=subtotal;priced+=i['costed_qty'];allocated+=len(i['selected'])
    result=dict(account=account,method='latest_eligible_shipments',method_authorized='User: Бери самые поздние подходящие отгрузки',
        cutoff_exclusive=analysis['cutoff_exclusive'],analysis_built_at=str(rows[0]['built_at']),
        physical_chain_proven=False,
        allocation_note='Поздние подходящие отгрузки, без повторного использования внутри аккаунта. Между складами с одинаковым составом распределение условное, в порядке названий складов.',
        items=selected,summary=dict(qty=sum(i['qty'] for i in selected),allocated_qty=allocated,costed_qty=priced,
            unmatched_qty=sum(i['unmatched_qty'] for i in selected),missing_cost_qty=allocated-priced,
            known_cost_rub=str(grand),complete=priced==sum(i['qty'] for i in selected)))
    db.upsert('wb_loss_cost_result',[dict(account=account,payload=Json(result),built_at=datetime.now(timezone.utc))],['account'])
    Path('data/wb_loss').mkdir(parents=True,exist_ok=True)
    Path(f'data/wb_loss/cost_{account}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(account=account,**result['summary']),ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--account',choices=['wb_acc1','wb_acc2'],required=True)
    calculate(parser.parse_args().account)
