# поток: ev
"""Official WB loss quantities and shipment candidates. Never reads or calculates cost.
Run: python -m ops.wb_loss_official import ARCHIVE; ... fetch; ... match
"""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import time
from zipfile import ZipFile
import requests
from psycopg2.extras import Json, execute_batch
from core import db

ACCOUNT = 'wb_acc1'
# MS dates are stored as instants. End of July 15 in Moscow (seller's business day).
CUTOFF = '2026-07-16T00:00:00+03:00'
ROW = re.compile(r'(?<!\d)(\d{8,10})\s+(\S+)\s+(\d+)\s*$')

def parse_pdf(blob):
    text = subprocess.run(['pdftotext', '-layout', '-', '-'], input=blob,
                          stdout=subprocess.PIPE, check=True).stdout.decode()
    if {'wb_acc1':'7807355364','wb_acc2':'7811803918'}[ACCOUNT] not in text or 'Отчет по утраченному товару' not in text:
        raise ValueError('Unexpected seller or document type')
    warehouse = re.search(r'по складу\s+([^\n]+)', text)
    date = re.search(r'^от\s+(\d{2}\.\d{2}\.\d{4})', text, re.M)
    if not warehouse or not date:
        raise ValueError('Missing report metadata')
    items = {}
    table_started = False
    for page, body in enumerate(text.split('\f'), 1):
        for line in body.splitlines():
            if 'Артикул Продавца' in line:
                table_started = True
            if not table_started:
                continue
            m = ROW.search(line)
            if not m:
                # Numeric WB column without a valid seller code/quantity is unsafe to skip.
                if re.search(r'\s{2,}\d{8,10}\s{2,}', line):
                    raise ValueError(f'Unparsed numeric row on page {page}')
                continue
            nm, code, qty = int(m[1]), m[2], int(m[3])
            if nm in items or qty <= 0 or code == '/':
                raise ValueError('Duplicate or invalid report row')
            items[nm] = dict(nm_id=nm, vendor_code=code, qty=qty, source_pages=[page])
    if not items:
        raise ValueError('Empty report')
    return warehouse[1].strip(), datetime.strptime(date[1], '%d.%m.%Y').date(), list(items.values())

def import_archive(path):
    documents = []
    with ZipFile(path) as outer:
        for member in outer.namelist():
            if not member.lower().endswith('.zip'):
                continue
            with ZipFile(BytesIO(outer.read(member))) as nested:
                names = [n for n in nested.namelist() if n.lower().endswith('.pdf')]
                if len(names) != 1:
                    raise ValueError('Expected one PDF per warehouse archive')
                blob = nested.read(names[0])
                warehouse, date, items = parse_pdf(blob)
                documents.append((hashlib.sha256(blob).hexdigest(), warehouse, date, items,
                                  member, names[0], blob, any(n.endswith('.sig') for n in nested.namelist())))
    if not documents or len({d[1] for d in documents}) != len(documents):
        raise ValueError('Empty archive or repeated warehouses')
    # Validate everything first; different documents require explicit reconciliation, never sum revisions.
    with db.get_conn() as conn, conn.cursor() as c:
        for digest, wh, date, items, member, pdf_name, blob, sig in documents:
            c.execute('SELECT id FROM wb_loss_document WHERE account=%s AND warehouse=%s', (ACCOUNT,wh))
            old=c.fetchone()
            if old and old[0] != digest:
                raise ValueError(f'Revised document requires reconciliation: {wh}')
            c.execute('''INSERT INTO wb_loss_document(id,account,warehouse,source_archive,source_member,
                pdf_name,header_date,pdf,signature_present) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING''',
                (digest,ACCOUNT,wh,str(Path(path).resolve()),member,pdf_name,date,blob,sig))
            execute_batch(c,'''INSERT INTO wb_loss_item(document_id,nm_id,vendor_code,qty,source_pages)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(document_id,nm_id) DO NOTHING''',
                [(digest,i['nm_id'],i['vendor_code'],i['qty'],i['source_pages']) for i in items])
    print(json.dumps({'documents':len(documents),'rows':sum(len(d[3]) for d in documents),
                      'qty':sum(i['qty'] for d in documents for i in d[3])}))

def request_page(source, cursor, method, path, **kwargs):
    cached=db.query('SELECT payload FROM wb_loss_search_page WHERE account=%s AND source=%s AND cursor=%s',
                    (ACCOUNT,source,str(cursor)))
    if cached:
        return cached[0]['payload']
    token=os.environ['WB_TOKEN_RETURNS_'+ACCOUNT.removeprefix('wb_').upper()]
    for attempt in range(6):
        r=requests.request(method,'https://marketplace-api.wildberries.ru'+path,
                           headers={'Authorization':token},timeout=90,**kwargs)
        if r.status_code in (429,500,502,503,504):
            time.sleep(min(60,max(5, int(r.headers.get('Retry-After','5')) if r.headers.get('Retry-After','5').isdigit() else 5)*(attempt+1)))
            continue
        if r.status_code != 200:
            raise RuntimeError(f'WB {source}: HTTP {r.status_code}')
        payload=r.json()
        db.upsert('wb_loss_search_page',[dict(account=ACCOUNT,source=source,cursor=str(cursor),payload=Json(payload))],['account','source','cursor'])
        time.sleep(0.4)
        return payload
    raise RuntimeError(f'WB retries exhausted: {source}')

def save_orders(source, orders):
    db.upsert('wb_loss_order_raw',[dict(account=ACCOUNT,order_id=o['id'],source=source,payload=Json(o))
              for o in orders],['account','order_id','source'])

def ms_bounds():
    cached=db.query("SELECT payload FROM wb_loss_search_page WHERE account=%s AND source='ms:history-boundary' AND cursor='0'",(ACCOUNT,))
    if cached:return cached[0]['payload']
    from core import ms_api as ms
    orgs=db.query("SELECT DISTINCT d.org FROM ms_demand_cogs d JOIN wb_cogs_demand w USING(demand_id) WHERE w.account=%s",(ACCOUNT,))
    if len(orgs)!=1:raise ValueError('Ambiguous MS organization')
    agent=ms.get('/entity/counterparty',{'filter':'name=Покупатель ВБ','limit':2})['rows']
    if len(agent)!=1:raise ValueError('Ambiguous MS agent')
    org=orgs[0]['org'];agent_href=agent[0]['meta']['href']
    r=ms.get('/entity/demand',{'filter':'organization='+ms.BASE+'/entity/organization/'+org+';agent='+agent_href,'order':'moment,asc','limit':1})
    result=dict(organization=org,agent_href=agent_href,earliest=r['rows'][0]['moment'],total=r['meta']['size'])
    db.upsert('wb_loss_search_page',[dict(account=ACCOUNT,source='ms:history-boundary',cursor='0',payload=Json(result))],['account','source','cursor'])
    return result

def fetch():
    # Read-only MS history boundary: include a month before the earliest WB shipment.
    bounds=ms_bounds()
    earliest=datetime.fromisoformat(bounds['earliest'])
    first=(earliest.year-1,12) if earliest.month==1 else (earliest.year,earliest.month-1)
    year,month=2026,7
    while (year,month)>=first:
        source=f'archive:{year}-{month:02d}'
        cursor=0; seen=set(); count=0
        while True:
            if cursor in seen: raise RuntimeError('Repeated WB pagination cursor')
            seen.add(cursor)
            payload=request_page(source,cursor,'GET','/api/marketplace/v3/fbs/orders/archive',
                                 params=dict(year=year,month=month,next=cursor,limit=1000))
            orders=payload['orders']; save_orders(source,orders); count+=len(orders)
            cursor=payload.get('next',0)
            if not cursor:break
        print(source,count,flush=True)
        year,month=(year-1,12) if month==1 else (year,month-1)
    source='current:2026-07-01:2026-07-16';cursor=0;seen=set();ids=[]
    start=int(datetime(2026,7,1,tzinfo=timezone.utc).timestamp())
    end=int(datetime.fromisoformat(CUTOFF).timestamp())
    while True:
        if cursor in seen:raise RuntimeError('Repeated WB pagination cursor')
        seen.add(cursor)
        payload=request_page(source,cursor,'GET','/api/v3/orders',params=dict(dateFrom=start,dateTo=end,next=cursor,limit=1000))
        orders=payload['orders'];save_orders(source,orders);ids.extend(o['id'] for o in orders)
        cursor=payload.get('next',0)
        if not cursor:break
    for offset in range(0,len(ids),1000):
        payload=request_page(source+':status',offset,'POST','/api/v3/orders/status',json={'orders':ids[offset:offset+1000]})
        save_orders(source+':status',payload['orders'])
    print('current',len(ids),flush=True)

class Catalogue:
    def __init__(self,models,sets,links):
        self.models=set(models);self.sets=sets;self.known=self.models|set(sets)
        self.graph=defaultdict(set)
        for a,b in links:
            if a and b:self.graph[a].add(b);self.graph[b].add(a)
    def code(self,code):
        code=str(code or '').strip()
        if code in self.known:return code
        if re.match(r'^\d{4}.+',code) and not re.fullmatch(r'\d{4}[XХxх]\d+',code) and code[:4] in self.known:
            return code[:4]
        return code
    def family(self,code):
        found={code};todo=[code]
        while todo:
            for other in self.graph[todo.pop()]-found:
                found.add(other);todo.append(other)
        return min(found)
    def expand(self,code,path=()):
        code=self.code(code)
        if code in path or len(path)>12:raise ValueError('Цикл состава ТК')
        if code not in self.sets:
            if code not in self.models:raise ValueError('Нет состава/модели в ТК: '+code)
            return Counter({code:1})
        parts=self.sets[code]
        if not parts:raise ValueError('Пустой состав ТК: '+code)
        out=Counter()
        for part in parts:
            if isinstance(part,str) and part.startswith('{'):
                part=ast.literal_eval(part)
            if isinstance(part,dict):
                # Collector snapshots may store model objects. Each entry is one component;
                # explicit quantities are respected only when valid.
                n=Decimal(str(part.get('quantity',part.get('qty',1))))
                if n<=0 or n!=int(n):raise ValueError('Некорректное количество ТК')
                part=part.get('external_code');n=int(n)
            else:n=1
            if not isinstance(part,str) or not part:raise ValueError('Некорректный компонент ТК')
            for c,q in self.expand(part,path+(code,)).items():out[c]+=q*n
        return out
    def signature(self,parts):
        out=Counter()
        for code,n in parts.items():out[self.family(code)]+=n
        return tuple(sorted(out.items()))


def load_catalogue():
    return Catalogue([r['external_code'] for r in db.query('SELECT external_code FROM prc_tc_model')],
        {r['external_code']:r['components'] for r in db.query('SELECT external_code,components FROM set_cost WHERE components IS NOT NULL')},
        [(r['external_code'],r['ref_code']) for r in db.query('SELECT external_code,ref_code FROM prc_tc_link')])

def fetch_ms():
    from core import ms_api as ms
    cat=load_catalogue()
    items=db.query('SELECT DISTINCT i.nm_id,i.vendor_code FROM wb_loss_item i JOIN wb_loss_document d ON d.id=i.document_id WHERE d.account=%s',(ACCOUNT,))
    nms={str(i['nm_id']) for i in items};signatures=set()
    for i in items:
        try:signatures.add(cat.signature(cat.expand(i['vendor_code'])))
        except (ValueError,SyntaxError):pass
    existing={r['demand_name'] for r in db.query("SELECT demand_name FROM ms_demand_cogs WHERE org=%s AND agent='Покупатель ВБ'",(ms_bounds()['organization'],))}
    cached={r['order_id'] for r in db.query('SELECT order_id FROM wb_loss_ms_lookup WHERE account=%s',(ACCOUNT,))}
    analyses=db.query('SELECT payload FROM wb_loss_analysis WHERE account=%s',(ACCOUNT,))
    force={e['order_id'] for a in analyses for i in a['payload']['items'] for e in i['evidence'] if e['eligibility']=='candidate' and not e['composition_ok']}
    todo={}
    for r in db.query("SELECT payload FROM wb_loss_order_raw WHERE account=%s AND source NOT LIKE '%%:status'",(ACCOUNT,)):
        o=r['payload'];key=str(o['id']);p=o.get('product',o)
        if key in cached:continue
        if key not in force and (key in existing or o.get('status',{}).get('wbStatus') not in ('canceled_by_client','defect')):continue
        relevant=str(p.get('nmId')) in nms
        if not relevant:
            try:relevant=cat.signature(cat.expand(p.get('article'))) in signatures
            except (ValueError,SyntaxError):pass
        if relevant:todo[key]=o
    bounds=ms_bounds()
    print('MS exact order lookups',len(todo),flush=True)
    for number,(key,o) in enumerate(sorted(todo.items(),key=lambda kv:kv[1].get('createdAt',''),reverse=True),1):
        filt='organization='+ms.BASE+'/entity/organization/'+bounds['organization']+';agent='+bounds['agent_href']+';name='+key
        raw=ms.get('/entity/demand',{'filter':filt,'limit':20,'expand':'positions.assortment'})
        if raw['meta']['size']>20:raise ValueError('Too many same-name MS demands')
        for d in raw['rows']:
            if d['positions']['meta']['size']>len(d['positions'].get('rows',[])):
                positions=ms.get('/entity/demand/'+d['id']+'/positions',{'limit':1000,'expand':'assortment'})
                if positions['meta']['size']>len(positions['rows']):raise ValueError('Incomplete MS positions')
                d['positions']=positions
        db.upsert('wb_loss_ms_lookup',[dict(account=ACCOUNT,order_id=key,payload=Json(raw))],['account','order_id'])
        if number%20==0 or number==len(todo):print('MS lookups',number,'/',len(todo),flush=True)
        time.sleep(0.15)


def match():
    items=db.query('''SELECT i.*,d.account,d.warehouse FROM wb_loss_item i
                      JOIN wb_loss_document d ON d.id=i.document_id WHERE d.account=%s''',(ACCOUNT,))
    orgs=db.query('''SELECT DISTINCT d.org FROM ms_demand_cogs d JOIN wb_cogs_demand w USING(demand_id)
                    WHERE w.account=%s''',(ACCOUNT,))
    if len(orgs)!=1:raise ValueError('Ambiguous MS organization')
    org=orgs[0]['org']
    cat=load_catalogue()
    # Collector stores MS naive local wall time as UTC. Verified against a live July 15 document.
    # Interpret that wall time as Moscow before applying the business-day cutoff.
    # Read actual shipment lines; no cost columns are selected.
    demands=db.query('''SELECT d.demand_id,d.demand_name,
        (d.moment AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Moscow') moment,d.qty,p.ms_id,p.qty position_qty,
        m.external_code,m.name FROM ms_demand_cogs d LEFT JOIN ms_demand_pos p USING(demand_id)
        LEFT JOIN ms_product m USING(ms_id) WHERE d.org=%s AND d.agent='Покупатель ВБ' AND (d.moment AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Moscow')<%s''',(org,CUTOFF))
    by_d={}
    for r in demands:
        d=by_d.setdefault(r['demand_id'],dict(demand_id=r['demand_id'],order_id=r['demand_name'],
                       moment=r['moment'].astimezone(timezone(timedelta(hours=3))).isoformat(),positions=[],parts=Counter(),errors=[]))
        d['positions'].append(dict(external_code=r['external_code'],qty=str(r['position_qty']),name=r['name']))
        try:
            if r['position_qty'] is None:raise ValueError('Нет позиций отгрузки')
            for code,n in cat.expand(r['external_code']).items():d['parts'][code]+=n*Decimal(r['position_qty'])
        except (ValueError,SyntaxError) as e:d['errors'].append(str(e))
    # Earlier exact-name documents queried from MS without invoking its costing endpoints.
    for row in db.query('SELECT payload FROM wb_loss_ms_lookup WHERE account=%s',(ACCOUNT,)):
        for raw in row['payload']['rows']:
            moment=datetime.fromisoformat(raw['moment']).replace(tzinfo=timezone(timedelta(hours=3)))
            if moment>=datetime.fromisoformat(CUTOFF):continue
            if raw['organization']['meta']['href'].split('/')[-1]!=org:raise ValueError('MS organization mismatch')
            d=dict(demand_id=raw['id'],order_id=raw['name'],moment=moment.isoformat(),positions=[],parts=Counter(),errors=[],ms_applied=raw.get('applicable'))
            if not raw.get('applicable'):d['errors'].append('Документ МС не проведён')
            for pos in raw['positions']['rows']:
                product=pos['assortment'];code=product.get('externalCode')
                d['positions'].append(dict(external_code=code,qty=str(pos['quantity']),name=product.get('name')))
                try:
                    for c,n in cat.expand(code).items():d['parts'][c]+=n*Decimal(str(pos['quantity']))
                except (ValueError,SyntaxError) as e:d['errors'].append(str(e))
            by_d[d['demand_id']]=d
    orders={};status={}
    for row in db.query('SELECT order_id,source,payload FROM wb_loss_order_raw WHERE account=%s ORDER BY fetched_at',(ACCOUNT,)):
        o=row['payload'];key=str(row['order_id'])
        if row['source'].endswith(':status'):status[key]=o
        else:orders[key]=dict(o,source=row['source'])
    for key,s in status.items():
        if key in orders:orders[key]['status']=s
    # Financial records supplement orders and identify later returns after a sold archive status.
    facts=defaultdict(lambda:dict(nm_ids=set(),operations=set(),srids=set()))
    for r in db.query('''SELECT DISTINCT payload->>'assembly_id' aid,payload->>'nm_id' nm,
           payload->>'supplier_oper_name' operation,payload->>'srid' srid
           FROM raw_wb_report WHERE account=%s AND coalesce(payload->>'assembly_id','0')<>'0' ''',(ACCOUNT,)):
        f=facts[r['aid']];f['nm_ids'].add(r['nm']);f['operations'].add(r['operation']);f['srids'].add(r['srid'])
    returns=defaultdict(list)
    for r in db.query('''SELECT demand_name,return_id,moment,store,ret_qty FROM ms_return_cogs
                       WHERE org=%s AND agent='Покупатель ВБ' ''',(org,)):
        returns[r['demand_name']].append(dict(return_id=r['return_id'],moment=str(r['moment']),store=r['store'],qty=str(r['ret_qty'])))
    indexed=defaultdict(list);exact=defaultdict(list)
    for d in by_d.values():
        key=d['order_id'];o=orders.get(key,{});p=o.get('product',o);s=o.get('status',{})
        f=facts[key];wb_status=s.get('wbStatus');supplier=s.get('supplierStatus')
        nm_ids=set(f['nm_ids'])
        if p.get('nmId'):nm_ids.add(str(p['nmId']))
        # Pre-handover cancellation is not proof of WB inventory. Preserve as excluded evidence.
        if returns[key]:eligibility='returned_to_seller'
        elif wb_status in ('declined_by_client','canceled') or supplier=='cancel':eligibility='early_cancel_or_seller_cancel'
        elif wb_status in ('canceled_by_client','defect') or 'Возврат' in f['operations']:eligibility='candidate'
        elif wb_status=='sold' or 'Продажа' in f['operations']:eligibility='sold_without_return'
        else:eligibility='status_unresolved'
        d.update(wb_status=wb_status,supplier_status=supplier,eligibility=eligibility,
                 source=o.get('source'),operations=sorted(x for x in f['operations'] if x),
                 return_documents=returns[key],wb_nm_ids=sorted(x for x in nm_ids if x),
                 wb_article=p.get('article'),created_at=o.get('createdAt'),srid=o.get('rid'))
        if not d['errors'] and d['parts']:indexed[cat.signature(d['parts'])].append(d)
        for nm in nm_ids:exact[nm].append(d)
    output=[]
    for i in items:
        try:
            bom=cat.expand(i['vendor_code']);sig=cat.signature(bom);error=None
        except (ValueError,SyntaxError) as e:
            bom=Counter();sig=None;error=str(e)
        candidates={d['demand_id']:d for d in exact[str(i['nm_id'])]}
        for d in indexed.get(sig,[]):candidates[d['demand_id']]=d
        evidence=[]
        for d in candidates.values():
            same_nm=str(i['nm_id']) in d['wb_nm_ids']
            composition_ok=bool(sig and not d['errors'] and cat.signature(d['parts'])==sig)
            e={k:v for k,v in d.items() if k not in ('parts',)}
            e['match_basis']='same_nm' if same_nm else 'tk_equivalent_composition'
            e['composition_ok']=composition_ok
            # Whole sold unit must match. No partial sets or surplus line quantities allocated.
            e['usable_candidate']=d['eligibility']=='candidate' and composition_ok
            evidence.append(e)
        evidence.sort(key=lambda e:(not e['usable_candidate'],e['moment']),reverse=False)
        usable=[e for e in evidence if e['usable_candidate']]
        output.append(dict(document_id=i['document_id'],nm_id=i['nm_id'],vendor_code=i['vendor_code'],
            warehouse=i['warehouse'],qty=i['qty'],bom=dict(bom),mapping_error=error,
            candidate_count=len(usable),evidence=evidence))
    shared=Counter(e['demand_id'] for i in output for e in i['evidence'] if e['usable_candidate'])
    for i in output:
        for e in i['evidence']:e['shared_with_report_rows']=shared[e['demand_id']] if e['usable_candidate'] else 0
    pages=db.query('SELECT source,count(*) pages,sum(jsonb_array_length(coalesce(payload->\'orders\',\'[]\'::jsonb))) orders FROM wb_loss_search_page WHERE account=%s GROUP BY source ORDER BY source',(ACCOUNT,))
    completed=db.query("SELECT source FROM wb_loss_search_page WHERE account=%s AND source LIKE 'archive:%%' AND (payload->>'next')::bigint=0 ORDER BY source",(ACCOUNT,))
    coverage=('Проверен архив '+completed[0]['source'][8:]+' — '+completed[-1]['source'][8:]+' ('+str(len(completed))+' месяцев). ') if completed else 'Архив ещё не проверен. '
    lookups=db.query("SELECT count(*) queries,coalesce(sum(jsonb_array_length(payload->'rows')),0) documents,count(*) FILTER(WHERE jsonb_array_length(payload->'rows')=0) missing FROM wb_loss_ms_lookup WHERE account=%s",(ACCOUNT,))[0]
    result=dict(cutoff_exclusive=CUTOFF,physical_chain_proven=False,cost_calculated=False,
        ms_history_from=min((d['moment'] for d in by_d.values()),default=None),
        history_note=coverage+'Подходящие документы дополнительно запрошены в МС по номеру задания. Дата изменения статуса и склад возврата в архиве не указаны. Архив и справочники не доказывают судьбу конкретной единицы.',
        ms_lookups=lookups,
        search_pages=pages,items=output,
        summary=dict(rows=len(output),qty=sum(i['qty'] for i in output),
          rows_with_candidates=sum(bool(i['candidate_count']) for i in output),
          rows_without_candidates=sum(not i['candidate_count'] for i in output),
          qty_without_candidates=sum(i['qty'] for i in output if not i['candidate_count']),
          mapping_errors=sum(bool(i['mapping_error']) for i in output),
          unique_candidate_demands=len(shared),shared_candidate_demands=sum(n>1 for n in shared.values())))
    # JSON conversion only, no cost arithmetic; quantities are strings in evidence where fractional.
    result=json.loads(json.dumps(result,default=str,ensure_ascii=False))
    db.upsert('wb_loss_analysis',[dict(account=ACCOUNT,payload=Json(result),built_at=datetime.now(timezone.utc))],['account'])
    Path('data/wb_loss').mkdir(parents=True,exist_ok=True)
    Path(f'data/wb_loss/analysis_{ACCOUNT}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result['summary'],ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['import','fetch','fetch-ms','match'])
    parser.add_argument('archive',nargs='?')
    parser.add_argument('--account',choices=['wb_acc1','wb_acc2'],default='wb_acc1')
    args=parser.parse_args()
    ACCOUNT=args.account
    if args.action=='import':import_archive(args.archive)
    elif args.action=='fetch':fetch()
    elif args.action=='fetch-ms':fetch_ms()
    else:match()
