# поток: cf
"""Read-only MS payment mirror for weekly cashflow; complete bounded snapshots only."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
from decimal import Decimal
from core import db, ms_api

ORGS = {'7807355364', '7811803918'}
BASE = Path('/opt/mp-analytics')


def collect(date_from=None, date_to=None):
    os.environ.setdefault('MS_API_PRIORITY', 'background')
    date_to = date_to or dt.date.today()
    date_from = date_from or date_to - dt.timedelta(days=date_to.weekday(), weeks=8)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive = BASE / '.private' / 'cashflow_ms_sources' / stamp
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    orgs = ms_api.get('/entity/organization', {'limit': 20})['rows']
    orgs = {o['inn']: o for o in orgs if o.get('inn') in ORGS}
    if set(orgs) != ORGS:
        raise ValueError('Missing cashflow organization')
    currencies = ms_api.get('/entity/currency', {'limit': 20})['rows']
    rub = {c['meta']['href'] for c in currencies if c.get('code') == '643'}
    if not rub:
        raise ValueError('Missing RUB currency')
    rows = []
    counts = {}
    for inn, org in sorted(orgs.items()):
        for entity in ['paymentin', 'paymentout']:
            docs, seen, offset = [], set(), 0
            expected = None
            while True:
                page = ms_api.get('/entity/' + entity, {
                    'limit': 20, 'offset': offset, 'order': 'moment,asc',
                    'filter': f"organization={org['meta']['href']};moment>={date_from} 00:00:00;moment<{date_to + dt.timedelta(days=1)} 00:00:00",
                    'expand': 'agent,expenseItem'})
                batch = page['rows']
                size = page['meta']['size']
                if expected is not None and expected != size:
                    raise ValueError('Payment collection changed during snapshot; retry next run')
                expected = size
                for d in batch:
                    day = dt.date.fromisoformat(d['moment'][:10])
                    if d['organization']['meta']['href'] != org['meta']['href'] or not date_from <= day <= date_to:
                        raise ValueError('Payment outside requested scope')
                    if d['id'] in seen:
                        raise ValueError('Duplicate MS payment in pagination')
                    if d['rate']['currency']['meta']['href'] not in rub:
                        raise ValueError('Non-RUB payment requires explicit currency conversion')
                    if not isinstance(d.get('applicable'), bool) or Decimal(str(d['sum'])) < 0:
                        raise ValueError('Invalid MS payment')
                    seen.add(d['id'])
                    docs.append(d)
                    rows.append((entity, d['id'], inn, day, json.dumps(d, ensure_ascii=False)))
                offset += len(batch)
                if len(batch) < 20:
                    break
            if len(docs) != expected:
                raise ValueError('Incomplete payment pagination')
            (archive / f'{inn}_{entity}.json').write_text(json.dumps(docs, ensure_ascii=False))
            counts[f'{inn}:{entity}'] = len(docs)
    # Publish both organizations together, never expose partial paging/failure as empty fact.
    from psycopg2.extras import execute_values
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute('select pg_advisory_xact_lock(80601110)')
        cur.execute('delete from cf_ms_payment where org_inn=any(%s) and day between %s and %s', (list(ORGS), date_from, date_to))
        if rows:
            execute_values(cur, '''insert into cf_ms_payment(entity,id,org_inn,day,payload) values %s
                on conflict(entity,id) do update set org_inn=excluded.org_inn, day=excluded.day,
                payload=excluded.payload, loaded_at=now()''', rows)
        for inn in sorted(ORGS):
            cur.execute('''insert into cf_ms_payment_sync(org_inn,date_from,date_to,row_count)
                values(%s,%s,%s,%s) on conflict(org_inn) do update set date_from=excluded.date_from,
                date_to=excluded.date_to,row_count=excluded.row_count,loaded_at=now()''',
                (inn,date_from,date_to,sum(v for k,v in counts.items() if k.startswith(inn+':'))))
    (archive / 'summary.json').write_text(json.dumps({'date_from': str(date_from), 'date_to': str(date_to), 'counts': counts}))
    print(f'[ok] MS payments: {len(rows)} documents, {date_from}–{date_to}', flush=True)
    return counts


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='date_from', type=dt.date.fromisoformat)
    ap.add_argument('--to', dest='date_to', type=dt.date.fromisoformat)
    a = ap.parse_args()
    collect(a.date_from, a.date_to)
