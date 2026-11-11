# поток: mkt / ozon
"""Read-only inventory audit using existing project clients; never calls their DB writers."""
import datetime as dt
import argparse
import json
import pathlib
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors import ozon as seller, ozon_ads as ads, ozon_products, ozon_bids

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--account', choices=['oz_acc1', 'oz_acc2'], default='oz_acc1')
parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path(__file__).parent)
parser.add_argument('--nonarchived-only', action='store_true')
parser.add_argument('--archived-only', action='store_true', help='Only archived catalog; no active-catalog refresh')
parser.add_argument('--catalog-only', action='store_true', help='Only Seller catalog and stocks; skip all advertising reads')
args = parser.parse_args()
if args.nonarchived_only and args.archived_only:
    parser.error('--nonarchived-only and --archived-only are mutually exclusive')
OUT = args.output.resolve()
if args.account != 'oz_acc1' and OUT == pathlib.Path(__file__).parent.resolve():
    parser.error('For another account supply a separate --output directory')
OUT.mkdir(parents=True, exist_ok=True)
if (OUT / 'acquisition_summary.json').exists():
    assert json.loads((OUT / 'acquisition_summary.json').read_text())['account'] == args.account, 'Output account mismatch'
RAW = OUT / 'raw'
RAW.mkdir(exist_ok=True)
PHASE = 'init'
SEQ = 0
original = requests.sessions.Session.request


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def guarded(self, method, url, **kw):
    global SEQ
    method = method.upper()
    path = '/' + url.split('/', 3)[-1]
    allowed = (
        method == 'POST' and url in [seller.PRODUCT_LIST_URL, seller.PRODUCT_INFO_URL,
                                    ads.PERF + '/api/client/token',
                                    ads.PERF + '/api/client/campaign/search_promo/v2/products']
    ) or (
        method == 'GET' and url.startswith(ads.PERF + '/api/client/campaign') and
        (path == '/api/client/campaign' or path.endswith('/v2/products'))
    )
    assert allowed, 'Only explicitly whitelisted inventory reads allowed'
    response = original(self, method, url, **kw)
    entry = {'account': args.account, 'at_utc': now(), 'phase': PHASE, 'method': method, 'path': path,
             'http': response.status_code}
    if not path.endswith('/token'):
        entry.update(body=kw.get('json'), params=kw.get('params'))
        SEQ += 1
        filename = f'raw/{PHASE}_{SEQ:04}.json'
        save(filename, {**entry, 'response': response.json()})
        entry['response_file'] = filename
    with (OUT / 'read_requests.jsonl').open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    if SEQ and SEQ % 10 == 0:
        print('Saved read', SEQ, 'phase', PHASE, 'HTTP', response.status_code, flush=True)
    return response


requests.sessions.Session.request = guarded
started = now()
header = seller._headers(args.account)
by_sku = {}
list_coverage = []
for visibility in (['ARCHIVED'] if args.archived_only else ['ALL'] if args.nonarchived_only else ['ALL', 'ARCHIVED']):
    PHASE = 'catalog_' + visibility.lower()
    pids = ozon_products._list_pids(header, visibility)
    assert len(pids) == len(set(pids)), 'Duplicate product IDs in listing'
    save('product_ids_' + visibility.lower() + '.json', pids)
    list_coverage.append({'visibility': visibility, 'product_count': len(pids)})
    print('Catalog', visibility, 'products', len(pids), flush=True)
    PHASE = 'info_' + visibility.lower()
    ozon_products._info(header, pids, visibility == 'ARCHIVED', by_sku)
    print('Catalog info', visibility, 'complete; known aliases', len(by_sku), flush=True)

if args.catalog_only:
    save('acquisition_summary.json', {'started_at_utc': started, 'finished_at_utc': now(),
                                    'account': args.account, 'catalog_lists': list_coverage,
                                    'known_sku_aliases': len(by_sku), 'scope': 'SELLER_CATALOG_ONLY',
                                    'marketplace_mutations': 0, 'database_data_writes': 0,
                                    'clients': ['ozon_products._list_pids', 'ozon_products._info']})
    print('READ-ONLY CATALOG COMPLETE', args.account, flush=True)
    raise SystemExit(0)

PHASE = 'performance_token'
headers = {'Authorization': 'Bearer ' + ads._token(args.account)}
PHASE = 'campaigns'
r = requests.get(ads.PERF + '/api/client/campaign', headers=headers, timeout=60)
r.raise_for_status()
campaigns = r.json()['list']
save('current_campaigns.json', {'account': args.account, 'retrieved_at_utc': now(), 'campaigns': campaigns})
coverage = []
active = set()
for c in campaigns:
    if c['state'] != 'CAMPAIGN_STATE_RUNNING' or c.get('advObjectType') != 'SKU':
        continue
    cid = str(c['id'])
    PHASE = 'active_products_' + cid
    products, code = ozon_bids._campaign_products(headers, cid)
    assert code == 200, 'Incomplete running campaign products'
    active.update(str(p['sku']) for p in products)
    save('active_products_' + cid + '.json', {'account': args.account, 'retrieved_at_utc': now(), 'http': code,
                                            'campaign_id': cid, 'products': products})
    coverage.append({'campaign_id': cid, 'products': len(products), 'http': code})
    print('Running CPC', cid, 'products', len(products), flush=True)
    time.sleep(0.2)

PHASE = 'search_promo'
promo = ozon_bids._search_promo(headers)
assert all(z['http'] == 200 for z in [json.loads(p.read_text()) for p in RAW.glob('search_promo_*.json')]), 'Incomplete search promo reads'
save('current_search_promo.json', {'account': args.account, 'retrieved_at_utc': now(), 'products': promo})
save('current_active_cpc.json', {'account': args.account, 'retrieved_at_utc': now(), 'skus': sorted(active), 'coverage': coverage})
save('acquisition_summary.json', {'started_at_utc': started, 'finished_at_utc': now(),
                                'account': args.account, 'catalog_lists': list_coverage,
                                'known_sku_aliases': len(by_sku), 'running_cpc_campaigns': len(coverage),
                                'running_cpc_skus': len(active), 'search_promo_pool_skus': len(promo),
                                'marketplace_mutations': 0, 'database_data_writes': 0,
                                'clients': ['ozon_products._list_pids', 'ozon_products._info',
                                            'ozon_bids._campaign_products', 'ozon_bids._search_promo']})
print('READ-ONLY INVENTORY COMPLETE; active CPC', len(active), 'search-promo pool', len(promo), flush=True)
