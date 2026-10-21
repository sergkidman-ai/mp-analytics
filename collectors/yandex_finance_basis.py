# поток: fin
"""Verified Yandex finance evidence. Refresh writes isolated candidates, never active prices.

python -m collectors.yandex_finance_basis --month YYYY-MM --output DIR
Report generation and stats/orders are read operations; no marketplace mutations or DB writes.
"""
import argparse
import calendar
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile
import requests
from core import db
from collectors import yandex_services as svc

D = Decimal
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'reports/data/yandex_finance_basis'
API = 'https://api.partner.market.yandex.ru'
EXPENSES = ('fee', 'delivery', 'transfer', 'promotion', 'agency', 'other_fee', 'subscription_cost')
CATEGORIES = {'commission':'fee','logistics':'delivery','acquiring':'transfer',
              'misc':'other_fee','subscription':'subscription_cost',
              'boost_sales':'promotion','boost_shows':'promotion','shelf':'promotion','reviews':'promotion'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def money(v):
    value = D(str(v or 0))
    if not value.is_finite(): raise RuntimeError('Non-finite monetary value')
    return value.quantize(D('.01'))


def floor_price(cogs, keep):
    from core.profit_floor import minimum_profit
    cost, ratio = D(str(cogs)), D(str(keep))
    if cost <= 0 or ratio <= 0 or ratio > 1:
        raise RuntimeError('Маркет: себестоимость/доля продавца непригодны')
    return (cost + minimum_profit(cost)) / ratio


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if path.exists():
            backup = path.with_suffix(path.suffix + '.previous')
            backup.write_bytes(path.read_bytes())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def load(path):
    return json.loads(Path(path).read_text())


def request(method, path, **kwargs):
    for attempt in range(3):
        r = requests.request(method, API + path, headers={'Api-Key':os.environ['YANDEX_API_KEY_ACC1']},
                             timeout=90, **kwargs)
        if r.status_code in (420,429) and attempt < 2:
            time.sleep(min(30, int(r.headers.get('Retry-After','10')))); continue
        r.raise_for_status()
        body = r.json()
        if body.get('status') not in (None,'OK'): raise RuntimeError('API returned unsuccessful status')
        return body.get('result', body)
    raise RuntimeError('API retry limit')


def report(kind, body, folder, seed_job=None, polls=3):
    """Persist jobs before polling. Resume PENDING; bounded retries, no repeated generation."""
    artifact = kind.replace('/', '_')
    jobpath = folder / (artifact + '_job.json')
    if jobpath.exists():
        previous = load(jobpath)
        statuspath = folder / (artifact + '_status.json')
        state = load(statuspath).get('status') if statuspath.exists() else None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(previous['requested_at'])).total_seconds()
        if state in ('DONE', 'FAILED') and age >= 86400:
            os.replace(jobpath, jobpath.with_suffix('.json.previous'))
    if not jobpath.exists():
        if seed_job:
            job = load(seed_job)
            job['seed_request_unverified'] = True
        else:
            job = request('POST', '/v2/reports/' + kind + '/generate', params={'format':'CSV'}, json=body)
        job.update({'requested_at':stamp(), 'request':body})
        save(jobpath, job)
    job = load(jobpath)
    if job['request'] != body: raise RuntimeError('Saved report job has different coverage')
    for n in range(polls):
        status = request('GET', '/reports/info/' + job['reportId'])
        # Download links may carry credentials; do not retain them in review artifacts.
        save(folder / (artifact + '_status.json'), {k:v for k,v in status.items() if k != 'file'})
        if status.get('status') == 'DONE':
            r = requests.get(status['file'], timeout=180)
            if r.status_code != 200: raise RuntimeError('Report download HTTP ' + str(r.status_code))
            (folder / (artifact + '.zip')).write_bytes(r.content)
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                sheets = {Path(name).name:list(csv.DictReader(io.TextIOWrapper(z.open(name),encoding='utf-8-sig')))
                          for name in z.namelist() if name.endswith('.csv')}
            save(folder / (artifact + '_sheets.json'), sheets)
            return sheets, {'status':'DONE','received_at':stamp(),'request':body,'report_id':job['reportId'],
                            'generated_at':status.get('generationFinishedAt'),
                            'generation_requested_at':status.get('generationRequestedAt'),
                            'seed_request_unverified':job.get('seed_request_unverified',False),
                            'sha256':hashlib.sha256(r.content).hexdigest()}
        if status.get('status') == 'FAILED': break
        if n + 1 < polls: time.sleep(3)
    return None, {'status':status.get('status'), 'request':body,'report_id':job['reportId']}


def fetch_orders(start, end, folder):
    camps = [x.strip() for x in os.environ['YANDEX_CAMPAIGN_ID_ACC1'].split(',') if x.strip()]
    orders, pages = {}, {}
    for cid in camps:
        token, seen, count = None, set(), 0
        while True:
            result = request('POST', f'/campaigns/{cid}/stats/orders',
                             params={'page_token':token} if token else {}, json={'dateFrom':start,'dateTo':end})
            if 'orders' not in result: raise RuntimeError('Missing orders collection')
            for order in result['orders']:
                oid = str(order['id'])
                if oid in orders and orders[oid] != order: raise RuntimeError('Conflicting duplicate order ' + oid)
                orders[oid] = order
            count += 1
            token = (result.get('paging') or {}).get('nextPageToken')
            if not token: break
            if token in seen or count >= 200: raise RuntimeError('Incomplete/cyclic pagination')
            seen.add(token); time.sleep(.5)
        pages[cid] = {'pages':count,'complete':True}
    meta = {'source':'stats/orders_calendar_payments','dateFrom':start,'dateTo':end,
            'campaigns':camps,'pages':pages,'received_at':stamp(),'orders':len(orders),'complete':True}
    save(folder/'orders.json',orders); save(folder/'orders_metadata.json',meta)
    return orders, meta


def payments(orders, month):
    seen, ids, rows = {}, {}, []
    for oid, order in orders.items():
        for p in order.get('payments') or []:
            if (p.get('date') or '')[:7] != month: continue
            if p.get('type') not in ('PAYMENT','REFUND'): raise RuntimeError('Unknown payment type')
            if not p.get('id'): raise RuntimeError('Payment ID missing')
            key = (str(oid), str(p['id']))
            if str(p['id']) in ids and ids[str(p['id'])] != str(oid):
                raise RuntimeError('Payment ID reused by different orders')
            ids[str(p['id'])] = str(oid)
            value = (p['date'][:10],p['type'],money(p['total']))
            if value[2] < 0: raise RuntimeError('Negative PAYMENT/REFUND total')
            if key in seen:
                if seen[key] != value: raise RuntimeError('Conflicting duplicate payment')
                continue
            seen[key] = value
            rows.append({'order_id':str(oid),'payment_id':str(p['id']),'date':value[0],
                         'type':value[1],'amount':value[2]})
    return rows


def reconcile(rows, closure):
    def key(r): return (str(r['order_id']).removesuffix('.0'),str(r['transaction_date'])[:10],
                        'PAYMENT' if r['category']=='revenue' else 'REFUND',abs(money(r['amount'])))
    old = Counter(key(r) for r in closure)
    new = Counter((r['order_id'],r['date'],r['type'],r['amount']) for r in rows)
    return {'closure_rows':sum(old.values()),'payments_rows':sum(new.values()),
            'unmatched_closure':sum((old-new).values()),'additional_payments':sum((new-old).values()),
            'missing':[{'key':k,'count':v} for k,v in (old-new).items()]}


def csv_money(v):
    return money(str(v or 0).replace('\xa0','').replace(' ','').replace(',','.'))


def service_rows(sheets, month):
    rows = []
    for fname, recs in sheets.items():
        for rec in recs:
            cat = svc._csv_cat(fname,rec.get('SERVICE_NAME'))
            if cat is None:
                if any(csv_money(rec.get(c)) for c in svc.SERVICE_PRICE_FALLBACK if rec.get(c)):
                    raise RuntimeError('Unclassified charged service sheet ' + fname)
                continue
            cost = csv_money(rec.get('TOTAL_AMOUNT')) if fname=='placement.csv' else next(
                (csv_money(rec[c]) for c in svc.SERVICE_PRICE_FALLBACK if rec.get(c) and csv_money(rec[c])),D(0))
            netting = csv_money(rec.get(svc.NETTING_COL))
            if not cost and not netting: continue
            day = next((rec[c] for c in svc.DATE_COLS if rec.get(c)),None)
            ym = svc._ym(day)
            if ym != month: raise RuntimeError('Service outside requested calendar month')
            rows.append({'category':cat,'cost':cost,'netting':netting,'svc_date':svc._svc_date(day)})
    return rows


def build(month, orders, metadata, services, service_meta, closure, closure_meta, cogs, ad_contract=0):
    rows = payments(orders,month); check = reconcile(rows,closure)
    monthly = {k:D(0) for k in EXPENSES}
    nett = defaultdict(lambda:D(0))
    promotion_parts = defaultdict(lambda:D(0))
    lines = {k:D(0) for k in EXPENSES}; netting = D(0)
    for r in services:
        if r['category'] not in CATEGORIES: raise RuntimeError('Unknown service category')
        lines[CATEGORIES[r['category']]] += money(r['cost']) + money(r['netting'])
        netting += money(r['netting'])
        monthly[CATEGORIES[r['category']]] += money(r['cost'])
        nett[CATEGORIES[r['category']]] += money(r['netting'])
        promotion_parts[r['category']] += money(r['cost'])
        promotion_parts['netting_'+r['category']] += money(r['netting'])
    lines['promotion'] += money(ad_contract)
    for order in orders.values():
        if (order.get('creationDate') or '')[:7] == month:
            lines['agency'] += sum((money(c.get('actual')) for c in order.get('commissions') or []
                                    if c.get('type')=='AGENCY'),D(0))
    monthly['agency'] = lines['agency']
    monthly['promotion'] += money(ad_contract)
    monthly.update(netting=netting, netting_fee=nett['fee'],netting_delivery=nett['delivery'],netting_promotion=nett['promotion'],
                   boost_sales=promotion_parts['boost_sales'],boost_shows=promotion_parts['boost_shows'],shelf=promotion_parts['shelf'],
                   netting_boost_sales=promotion_parts['netting_boost_sales'],netting_boost_shows=promotion_parts['netting_boost_shows'],
                   netting_shelf=promotion_parts['netting_shelf'],reviews_cost=promotion_parts['reviews'],ad_contract=money(ad_contract))
    revenue = sum((r['amount'] for r in rows if r['type']=='PAYMENT'),D(0))
    refunds = sum((r['amount'] for r in rows if r['type']=='REFUND'),D(0))
    turnover = revenue + netting; expense = sum(lines.values(),D(0))
    expected = set(x.strip() for x in os.environ.get('YANDEX_CAMPAIGN_ID_ACC1','').split(',') if x.strip())
    complete = (metadata.get('complete') is True and set(metadata.get('campaigns',[])) == expected
                and all(p.get('complete') is True for p in metadata.get('pages',{}).values())
                and set(metadata.get('pages',{})) == expected and bool(expected)
                and not metadata.get('missing_historical_orders')
                and metadata.get('dateFrom','9999') <= month+'-01'
                and metadata.get('dateTo','') >= month+f'-{calendar.monthrange(int(month[:4]),int(month[5:]))[1]:02d}')
    ready = complete and service_meta.get('status')=='DONE' and check['unmatched_closure']==0 and turnover>0 and 0<=expense<turnover
    closure_matched = not closure_meta.get('seed_request_unverified') and closure_meta.get('status')=='DONE' and check['additional_payments']==0 and check['unmatched_closure']==0
    basis = {'account':'ya_acc1','month':month,'calendar_closed':month<date.today().strftime('%Y-%m'),
             'accounting_provisional':not closure_matched,'price_usable':bool(ready),
             'status':'verified_closure' if closure_matched and ready else ('verified_payments_pending_closure' if ready else 'unverified'),
             'source':'closure_reconciled' if closure_matched else 'stats/orders_calendar_payments',
             'monthly_record':dict(monthly,revenue=revenue,returns_sum=refunds),
             'received_at':metadata.get('received_at'),'coverage':metadata,'services':service_meta,'closure':closure_meta,
             'reconciliation':check,'lines':dict(lines,revenue=revenue,netting=netting,returns_sum=refunds),
             'turnover':turnover,'expenses':expense,'cogs':money(cogs),
             'profit_before_opex_tax':turnover-expense-refunds-money(cogs),
             'retention':expense/turnover if turnover>0 else None,'keep':(turnover-expense)/turnover if turnover>0 else None}
    basis['version'] = hashlib.sha256(json.dumps(basis,default=str,sort_keys=True).encode()).hexdigest()
    return basis, rows


def verified_month(month):
    path = DATA / (month+'.json')
    if not path.exists(): return None
    basis = load(path)
    return basis if basis.get('price_usable') is True and basis.get('month')==month and basis.get('account')=='ya_acc1' else None


def active_basis(path=None):
    """Activation is a separate, explicitly authorized operation. Never read UI hist."""
    path = Path(path) if path else DATA / 'active.json'
    if not path.exists(): raise RuntimeError('Маркет: активная проверенная финансовая база не опубликована')
    b = load(path)
    previous = (date.today().replace(day=1) - __import__('datetime').timedelta(days=1)).strftime('%Y-%m')
    if b.get('account')!='ya_acc1' or b.get('price_usable') is not True or b.get('month')!=previous:
        raise RuntimeError('Маркет: база непригодна или относится к другому месяцу')
    if not b.get('version') or b.get('retention') is None or not 0 <= D(str(b['retention'])) < 1:
        raise RuntimeError('Маркет: некорректная версия/ставка базы')
    return b


def refresh(month, folder, seed_job=None):
    folder = Path(folder); folder.mkdir(parents=True,exist_ok=True)
    with lock(folder/'.refresh.lock'):
        y,m = int(month[:4]),int(month[5:]); end = f'{month}-{calendar.monthrange(y,m)[1]:02d}'
        cid = os.environ['YANDEX_CAMPAIGN_ID_ACC1'].split(',')[0].strip()
        csvs, cm = report('closure-documents/detalization',{'campaignId':int(cid),'monthOfYear':{'year':y,'month':m},'contractType':'INCOME'},folder,seed_job)
        from collectors.yandex_closure import _parse_csvs
        old = db.query('SELECT * FROM raw_yandex_closure WHERE account=%s AND ym=%s',('ya_acc1',month))
        closure = _parse_csvs(csvs,'ya_acc1',month,cid) if csvs is not None else old
        save(folder/'closure_rows.json',closure)
        earliest = db.query("SELECT min(payload->>'creationDate') oldest FROM raw_yandex_stats_order WHERE account='ya_acc1'")[0]['oldest']
        start = min((earliest or month+'-01')[:10], month+'-01')
        orders, meta = fetch_orders(start,date.today().isoformat(),folder)
        # All known historic closure order IDs must be in the new order perimeter.
        historical = db.query("SELECT DISTINCT order_id FROM raw_yandex_closure WHERE account='ya_acc1' AND ym<=%s",(month,))
        missing = sorted({str(r['order_id']).removesuffix('.0') for r in historical if r['order_id']} - set(orders))
        meta['missing_historical_orders'] = missing
        if missing: meta['complete'] = False
        save(folder/'orders_metadata.json',meta)
        sheets, sm = report('united-marketplace-services',{'businessId':int(os.environ['YANDEX_BUSINESS_ID_ACC1']),
                           'dateTimeFrom':month+'-01T00:00:00+03:00','dateTimeTo':end+'T23:59:59+03:00'},folder,polls=12)
        if sheets is None: raise RuntimeError('Services report not DONE; no verified candidate')
        services = service_rows(sheets,month); save(folder/'services_rows.json',services)
        fm = db.query('SELECT * FROM yandex_finance_monthly WHERE account=%s AND month=%s',('ya_acc1',month+'-01'))
        if not fm: raise RuntimeError('Missing existing FIFO monthly COGS')
        basis, rows = build(month,orders,meta,services,sm,closure,cm,fm[0]['cogs'],fm[0].get('ad_contract',0))
        save(folder/'payments.json',rows); save(folder/'candidate.json',basis)
        return basis


def publish_report_evidence(basis):
    """An unsuccessful new refresh must not erase previously verified closed-month data."""
    month = basis['month']
    save(DATA/'latest_refresh_status.json', {'month':month,'status':basis['status'],
         'price_usable':basis['price_usable'],'checked_at':stamp(),'candidate_version':basis['version']})
    if basis.get('price_usable') is True:
        save(DATA/(month+'.json'),basis)


def refresh_previous():
    previous = (date.today().replace(day=1)-__import__('datetime').timedelta(days=1)).strftime('%Y-%m')
    # Serialize refresh and publication; this is REPORT evidence, not the trading pointer.
    with lock(DATA/'.evidence.lock'):
        b = refresh(previous, DATA/'runs'/previous)
        publish_report_evidence(b)
    return b['status']


def main():
    a=argparse.ArgumentParser();a.add_argument('--month',required=True);a.add_argument('--output',required=True);a.add_argument('--seed-job')
    args=a.parse_args(); b=refresh(args.month,args.output,args.seed_job)
    print(json.dumps({k:b[k] for k in ('month','status','turnover','expenses','retention','version')},default=str))

if __name__=='__main__': main()
