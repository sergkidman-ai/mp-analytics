# поток: prc
"""Recover dates from MS CREATE audit events; writes only local evidence with --apply.
UUIDv1 only shortlists/orders cards and verifies identity, never supplies a published date.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone, timedelta
import json
from pathlib import Path
from uuid import UUID
from core import db, ms_api

EPOCH = datetime(1582, 10, 15, tzinfo=timezone.utc)
MS_ZONE = timezone(timedelta(hours=3))


def uuid_time(value):
    uid = UUID(str(value))
    # MS uses UUID time fields with its own variant bits (often 0a80).
    if uid.fields[2] >> 12 != 1:
        raise ValueError('not a time-based product ID')
    return EPOCH + timedelta(microseconds=uid.time // 10)


def oldest_card(cards):
    return min(cards, key=lambda c: uuid_time(c['ms_id']))


def creation_event(card, fetch=ms_api.get):
    path = '/entity/product/' + str(card['ms_id']) + '/audit'
    pages = [fetch(path, {'limit':20})]
    size = pages[0].get('meta',{}).get('size',0)
    if size > len(pages[0].get('rows',[])):
        pages.append(fetch(path, {'limit':20,'offset':max(0,size-20)}))
    events = [e for page in pages for e in page.get('rows',[]) if e.get('eventType') == 'create']
    if len(events) == 2 and events[0] == events[1]:
        events = events[:1]
    if len(events) != 1:
        raise ValueError('unambiguous CREATE event not found')
    event = events[0]
    if event.get('entity',{}).get('meta',{}).get('href','').rstrip('/').split('/')[-1] != str(card['ms_id']):
        raise ValueError('CREATE belongs to another product')
    moment = datetime.fromisoformat(event['moment']).replace(tzinfo=MS_ZONE)
    if abs((moment - uuid_time(card['ms_id'])).total_seconds()) > 300:
        raise ValueError('CREATE moment and product identity disagree')
    return moment.date(), event, pages


def apply_evidence(records, allow_earlier=False):
    """Only verified CREATE dates; a complete catalogue may prove an earlier date."""
    result={'restored':0,'already_known':0,'earlier_sale':0}
    if allow_earlier: result['corrected']=0
    with db.get_conn() as conn, conn.cursor() as c:
        c.execute('''SELECT p.external_code,min(d.moment)::date FROM ms_demand_pos pos
                     JOIN ms_demand_cogs d USING(demand_id)
                     JOIN ms_product p ON p.ms_id=pos.ms_id
                     WHERE p.external_code=ANY(%s) GROUP BY 1''',
                  ([r['external_code'] for r in records] or [''],))
        sales=dict(c.fetchall())
        for r in records:
            new_date=date.fromisoformat(r['ms_created'])
            c.execute('SELECT ms_created FROM prc_launch WHERE external_code=%s FOR UPDATE',(r['external_code'],))
            row=c.fetchone()
            if row is None or (row[0] is not None and (not allow_earlier or row[0] <= new_date)):
                result['already_known']+=1;continue
            sale=sales.get(r['external_code'])
            if sale and sale < new_date:
                result['earlier_sale']+=1;continue
            c.execute('''INSERT INTO prc_launch_creation_evidence(external_code,ms_created,source_ref)
                         VALUES(%s,%s,%s) ON CONFLICT(external_code) DO UPDATE SET
                         ms_created=excluded.ms_created,source_ref=excluded.source_ref
                         WHERE excluded.ms_created < prc_launch_creation_evidence.ms_created''',
                      (r['external_code'],r['ms_created'],r['source_ref']))
            c.execute('''UPDATE prc_launch l SET ms_created=e.ms_created,ms_source='documented'
                         FROM prc_launch_creation_evidence e
                         WHERE l.external_code=e.external_code AND l.external_code=%s
                           AND (l.ms_created IS NULL OR (%s AND e.ms_created < l.ms_created))''',
                      (r['external_code'],allow_earlier))
            result['corrected' if row[0] is not None else 'restored']+=c.rowcount
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--since',default='2026-06-14')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    # Bound only this standalone read-only process; leave shared services unchanged.
    ms_api.TIMEOUT = 25
    ms_api.RETRIES = 2
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    cards=db.query("SELECT ms_id::text,code,external_code,archived FROM ms_product WHERE external_code ~ '^[0-9]{4}$'")
    groups={}
    for card in cards:groups.setdefault(card['external_code'],[]).append(card)
    since=date.fromisoformat(args.since);today=datetime.now(MS_ZONE).date()
    candidates=[];ambiguous=[]
    for ext,items in groups.items():
        try:
            first=oldest_card(items);day=uuid_time(first['ms_id']).astimezone(MS_ZONE).date()
        except (ValueError,AttributeError):
            ambiguous.append(ext);continue
        if since <= day <= today:candidates.append(first)
    (output/'candidates.json').write_text(json.dumps({'cards':candidates,'ambiguous_id_models':ambiguous},ensure_ascii=False,default=str))
    print('candidate models',len(candidates),'ambiguous IDs',len(ambiguous),flush=True)
    def check(card):
        path=output/(card['external_code']+'.json')
        if path.exists():return json.loads(path.read_text())
        try:
            day,event,pages=creation_event(card)
            result=dict(external_code=card['external_code'],ms_id=card['ms_id'],ms_created=day.isoformat(),
                        source_ref=str(path.resolve()),event=event,audit_pages=pages)
        except Exception as e:result=dict(external_code=card['external_code'],error=type(e).__name__+': '+str(e)[:200])
        path.write_text(json.dumps(result,ensure_ascii=False,default=str));return result
    records=[];errors=[]
    with ThreadPoolExecutor(max_workers=min(4,max(1,args.workers))) as pool:
        futures=[pool.submit(check,c) for c in candidates]
        for i,f in enumerate(as_completed(futures),1):
            r=f.result();(errors if 'error' in r else records).append(r)
            if i%25==0:print('checked',i,'of',len(candidates),'verified',len(records),'unresolved',len(errors),flush=True)
    summary={'verified':len(records),'unresolved':len(errors),'ambiguous_ids':len(ambiguous),'since':str(since),'errors':errors}
    if args.apply:summary['applied']=apply_evidence(records)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,default=str));print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
