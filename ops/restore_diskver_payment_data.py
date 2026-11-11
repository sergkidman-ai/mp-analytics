# поток: inv
"""One-time MS recovery. Uses the existing po_payment_watch, never a bank sender.

Default: build a preview in a rolled-back transaction. --apply requires the same
snapshot and verifies that the resulting history/drafts equal the preview.
"""
import argparse
import ast
import contextlib
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ORG = '7811803918'
MS = 'https://api.moysklad.ru/api/remap/1.2'
HOLD = 'ТРЕБУЕТСЯ СВЕРКА СО СБЕРОМ'


def history_rows(payments, supplier_inns, org_href):
    rows = []
    identities = set()
    for payment in payments:
        if payment.get('organization', {}).get('meta', {}).get('href') != org_href:
            raise ValueError('Payment organization mismatch')
        if payment['id'] in identities:
            raise ValueError('Duplicate MS payment identity')
        identities.add(payment['id'])
        agent = payment.get('agent') or {}
        if agent.get('inn') not in supplier_inns or not payment.get('applicable'):
            continue
        amount = Decimal(str(payment['sum'])) / 100
        if amount <= 0:
            raise ValueError('Invalid outgoing payment amount')
        orders = sorted({op['purchaseOrder']['meta']['href'].rsplit('/', 1)[1]
                         for op in payment.get('operations', []) if op.get('purchaseOrder')})
        for op in payment.get('operations', []):
            if op.get('organization', {}).get('meta', {}).get('href') != org_href:
                raise ValueError('Payment operation organization mismatch')
            if op.get('agent', {}).get('meta', {}).get('href') != agent.get('meta', {}).get('href'):
                raise ValueError('Payment operation supplier mismatch')
        rows.append({'ms_payment_id': payment['id'], 'inn': agent['inn'],
                     'name': agent.get('name', agent['inn']), 'amount': str(amount),
                     'covers_po_ids': orders, 'moment': payment['moment'],
                     'purpose': payment.get('paymentPurpose') or '', 'number': payment.get('name') or '',
                     'sync_id': payment.get('syncId')})
    return sorted(rows, key=lambda row: row['ms_payment_id'])


class ReadonlyMS:
    def __init__(self, token, directory):
        import requests
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'gzip'})
        self.directory = directory
        directory.mkdir(mode=0o700, exist_ok=True)

    def get(self, path):
        import time
        parsed = urlsplit(path)
        if parsed.scheme:
            if not path.startswith(MS + '/'):
                raise ValueError('Only MS reads are allowed')
            route = parsed.path.removeprefix('/api/remap/1.2')
        else:
            route = parsed.path
        params = {key: value[-1] for key, value in parse_qs(parsed.query).items()}
        count = min(int(params.get('limit', 20)), 100)
        start = int(params.get('offset', 0))
        combined = None
        offset = 0
        while offset < count:
            query = dict(params, limit=min(20, count - offset), offset=start + offset)
            url = MS + route
            identity = hashlib.sha256((url + '?' + urlencode(query)).encode()).hexdigest()
            cached = self.directory / (identity + '.json')
            if cached.exists():
                data = json.loads(cached.read_text())
            else:
                for attempt in range(3):
                    response = self.session.get(url, params=query, timeout=45, allow_redirects=False)
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
                    time.sleep(2 + 2 * attempt)
                if response.status_code != 200:
                    raise RuntimeError('MS read failed, HTTP ' + str(response.status_code))
                data = response.json()
                cached.write_text(json.dumps(data, ensure_ascii=False))
            if 'rows' not in data:
                return data
            if combined is None:
                combined = dict(data, rows=[])
            combined['rows'].extend(data['rows'])
            if len(data['rows']) < int(query['limit']):
                break
            offset += int(query['limit'])
        return combined


def restore(snapshot, apply=False):
    import os
    import urllib.request
    import psycopg2
    from psycopg2.extras import Json
    from dotenv import dotenv_values
    os.umask(0o077)
    sys.path.insert(0, str(ROOT))
    import invoice_bot
    invoice_bot.__path__ = [str(ROOT / 'invoice_bot')]
    from core import db
    from invoice_bot import seed_payment_terms as seed, po_payment_watch as watch
    from invoice_bot import payment_integrity_alert as alert

    cfg = dotenv_values(ROOT / '.env')
    gates = ('PAYMENT_AUTOSEND', 'ALFA_PAYMENT_APPLY', 'ALFA_PAYMENT_PROD_READY',
             'SBER_PAYMENT_APPLY', 'SBER_PAYMENT_PROD_READY')
    if any((cfg.get(key) or '').lower().strip() in ('1', 'true', 'yes', 'on') for key in gates):
        raise RuntimeError('Payment gates must remain disabled during recovery')
    meta = json.loads((snapshot / 'snapshot.json').read_text())
    if meta['org_inn'] != ORG or not meta.get('complete'):
        raise ValueError('Incomplete or wrong-organization snapshot')
    payments = json.loads((snapshot / 'paymentout_all.json').read_text())
    orders = json.loads((snapshot / 'purchaseorder_all.json').read_text())
    href = MS + '/entity/organization/' + meta['org_id']
    if any(o.get('organization', {}).get('meta', {}).get('href') != href for o in orders):
        raise ValueError('Order organization mismatch')
    history = history_rows(payments, {row[1] for row in seed.TERMS}, href)
    client = ReadonlyMS(cfg['MOYSKLAD_TOKEN'], snapshot / 'additional_ms_reads')
    terms = []
    for name, inn, method, days, cap, advance, threshold, hint in seed.TERMS:
        if inn == '9718075418':
            days = 6
        if inn == '9717092410':
            method, days, cap, advance, threshold = 'prepayment_balance', None, None, 25000, 10000
        with patch.object(seed, 'get', client.get):
            agent, resolved_name = seed.resolve_agent(inn, hint)
        if not agent:
            raise ValueError('Supplier not found in MS: ' + inn)
        terms.append((inn, resolved_name or name, method, days, cap, advance, threshold, agent,
                      2 if inn in ('9718075418', '7840480595') else 1))

    def snapshot_orders(inn, since, agent_id=None):
        return [o for o in orders if o.get('applicable') and o['moment'][:10] >= str(since)
                and o.get('agent', {}).get('inn') == inn
                and (not agent_id or o.get('agent', {}).get('id') == agent_id)]

    original_urlopen = urllib.request.urlopen

    def read_only_urlopen(request, *args, **kwargs):
        method = request.get_method() if hasattr(request, 'get_method') else 'GET'
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        if method != 'GET' or not url.startswith(MS + '/'):
            raise RuntimeError('External writes and bank requests are disabled in recovery')
        return original_urlopen(request, *args, **kwargs)

    conn = psycopg2.connect(cfg['DATABASE_URL'], application_name='diskver_payment_recovery')

    @contextlib.contextmanager
    def shared_connection():
        yield conn

    mode = 'apply' if apply else 'preview'
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (7811803918,))
            cur.execute('SELECT count(*) FROM supplier_payment_credit WHERE org_inn=%s', (ORG,))
            if cur.fetchone()[0]:
                raise RuntimeError('Registered credits require a separate reconciliation')
            for inn, name, method, days, cap, advance, threshold, agent, delivery in terms:
                cur.execute('''INSERT INTO supplier_payment_terms
                    (org_inn,inn,name,method,deferral_days,payment_cap,advance_amount,
                     balance_threshold,ms_agent_id,delivery_days,active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
                    ON CONFLICT (org_inn,inn) DO UPDATE SET name=excluded.name,
                     method=excluded.method,deferral_days=excluded.deferral_days,
                     payment_cap=excluded.payment_cap,advance_amount=excluded.advance_amount,
                     balance_threshold=excluded.balance_threshold,ms_agent_id=excluded.ms_agent_id,
                     delivery_days=excluded.delivery_days,active=true,updated_at=now()''',
                            (ORG, inn, name, method, days, cap, advance, threshold, agent, delivery))
            cur.execute('SELECT coalesce(min(id),0)-1 FROM payment_draft_queue WHERE id<0')
            negative = cur.fetchone()[0]
            for row in history:
                cur.execute('SELECT id FROM payment_draft_queue WHERE org_inn=%s AND ms_payment_id=%s',
                            (ORG, row['ms_payment_id']))
                if cur.fetchone():
                    continue
                cur.execute('''INSERT INTO payment_draft_queue
                    (id,org_inn,inn,kind,amount,covers_po_ids,status,ms_payment_id,note,
                     created_at,purpose_text,payee,idem_key)
                    VALUES (%s,%s,%s,'ms_payment',%s,%s,'paid',%s,%s,%s,%s,%s,%s)''',
                            (negative, ORG, row['inn'], row['amount'], row['covers_po_ids'],
                             row['ms_payment_id'], 'Проведён в МС, платёж №' + row['number'],
                             row['moment'] + '+03:00', row['purpose'],
                             Json({'name': row['name'], 'inn': row['inn'], 'source': 'moysklad'}),
                             'ms-payment:' + ORG + ':' + row['ms_payment_id']))
                negative -= 1
        logfile = snapshot / (mode + '_watch.log')
        with logfile.open('w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log), \
                patch.object(db, 'get_conn', shared_connection), \
                patch.object(watch, '_fetch_purchase_orders', snapshot_orders), \
                patch.object(watch, 'get_r', client.get), \
                patch.object(watch.alfa_link, 'get', client.get), \
                patch.object(alert, 'notify_assignment', return_value=False), \
                patch('urllib.request.urlopen', read_only_urlopen):
            watch.set_org(ORG)
            watch.LOOKBACK_DAYS = (datetime.now().date() - datetime.fromisoformat(meta['start']).date()).days
            watch.run()
        errors = [line for line in logfile.read_text().splitlines() if 'ОШИБКА' in line]
        with conn.cursor() as cur:
            cur.execute('''UPDATE payment_draft_queue SET recovery_hold=true,
                note=coalesce(note,'') || %s
                WHERE org_inn=%s AND status='planned' AND NOT recovery_hold''',
                        (' | ' + HOLD + ': черновики банка, НДС и сальдо авансов', ORG))
            cur.execute('''SELECT inn,kind,amount::text,covers_po_ids,status,recovery_hold
                FROM payment_draft_queue WHERE org_inn=%s ORDER BY kind,inn,amount,covers_po_ids''', (ORG,))
            queue = cur.fetchall()
            cur.execute('''SELECT status,count(*),sum(amount)::text FROM po_payment_status
                WHERE org_inn=%s GROUP BY status ORDER BY status''', (ORG,))
            statuses = cur.fetchall()
            cur.execute('SELECT count(*) FROM supplier_payment_terms WHERE org_inn=%s', (ORG,))
            term_count = cur.fetchone()[0]
        report = {'org_inn': ORG, 'snapshot': str(snapshot), 'terms': term_count,
                  'history_count': len(history), 'queue': queue, 'order_statuses': statuses,
                  'watch_errors': errors, 'applied': apply}
        fingerprint = hashlib.sha256(json.dumps([queue, statuses], ensure_ascii=False).encode()).hexdigest()
        report['fingerprint'] = fingerprint
        if apply:
            prior = json.loads((snapshot / 'recovery_preview.json').read_text())
            if fingerprint != prior['fingerprint']:
                raise RuntimeError('Recovery result differs from preview; transaction rolled back')
            conn.commit()
        else:
            conn.rollback()
        (snapshot / ('recovery_' + mode + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'applied': apply, 'terms': term_count, 'history': len(history),
                          'queue_rows': len(queue), 'planned': sum(r[4] == 'planned' for r in queue),
                          'order_statuses': statuses, 'watch_error_count': len(errors)}, ensure_ascii=False))
    finally:
        conn.rollback()
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    restore(args.snapshot, args.apply)
