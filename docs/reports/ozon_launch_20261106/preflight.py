# поток: mkt / ozon
"""Refresh only the frozen 9242 target cards and individual CPC minima. No ad writes."""
import collections
import argparse
import csv
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors import ozon as seller, ozon_ads as ads, ozon_bids

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--account', choices=['oz_acc1', 'oz_acc2'], default='oz_acc1')
parser.add_argument('--output', type=Path, default=Path(__file__).parent)
parser.add_argument('--source', type=Path)
args = parser.parse_args()
OUT = args.output.resolve()
if args.account != 'oz_acc1' and (OUT == Path(__file__).parent.resolve() or args.source is None):
    parser.error('Other account requires separate --output and explicit --source')
OUT.mkdir(parents=True, exist_ok=True)
RAW = OUT / 'preflight_raw'
RAW.mkdir(parents=True, exist_ok=True)
SOURCE = args.source or (OUT.parent / 'ozon_wave_revision_20261106/wave_draft_all_candidates.csv')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


original = requests.sessions.Session.request
seq = 0


def guard(self, method, url, **kw):
    global seq
    method = method.upper()
    allowed = (method == 'POST' and url in [seller.PRODUCT_INFO_URL, ads.PERF + '/api/client/token',
               ads.PERF + '/api/client/min/sku']) or (method == 'GET' and url.startswith(ads.PERF + '/api/client/campaign'))
    assert allowed, 'Preflight permits reads only'
    r = original(self, method, url, **kw)
    if not url.endswith('/token'):
        seq += 1
        entry = {'account': args.account, 'at_utc': now(), 'method': method, 'path': url.split('.ru', 1)[-1],
                 'http': r.status_code, 'request': kw.get('json'), 'params': kw.get('params')}
        file = RAW / f'read_{dt.datetime.now().strftime("%H%M%S%f")}_{seq:04}.json'
        save(file, {**entry, 'response': r.json()})
        with (OUT / 'preflight_reads.jsonl').open('a') as f:
            f.write(json.dumps({**entry, 'response_file': str(file.relative_to(OUT))}, ensure_ascii=False) + '\n')
    return r


requests.sessions.Session.request = guard
with SOURCE.open(encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
assert rows and all(r.get('account', 'oz_acc1') == args.account for r in rows), 'Source account mismatch'
if (OUT / 'preflight_summary.json').exists():
    assert json.loads((OUT / 'preflight_summary.json').read_text()).get('account', 'oz_acc1') == args.account
h = {'Authorization': 'Bearer ' + ads._token(args.account)}
sh = seller._headers(args.account)
r = requests.get(ads.PERF + '/api/client/campaign', headers=h, timeout=90)
r.raise_for_status()
campaigns = r.json()['list']
save(OUT / 'campaigns_before.json', {'account': args.account, 'at_utc': now(), 'campaigns': campaigns})
active = collections.defaultdict(set)
for c in campaigns:
    if c['state'] == 'CAMPAIGN_STATE_RUNNING' and c.get('advObjectType') == 'SKU':
        products, code = ozon_bids._campaign_products(h, str(c['id']))
        assert code == 200
        for p in products:
            active[str(p['sku'])].add(str(c['id']))
save(OUT / 'running_cpc_skus_before.json', {k: sorted(v) for k, v in active.items()})
items = {}
pids = [r['product_id'] for r in rows]
for i in range(0, len(pids), 1000):
    file = OUT / f'cards_batch_{i // 1000:02}.json'
    if file.exists():
        obj = json.loads(file.read_text())
    else:
        r = requests.post(seller.PRODUCT_INFO_URL, headers=sh, json={'product_id': pids[i:i + 1000]}, timeout=90)
        r.raise_for_status()
        obj = {'account': args.account, 'at_utc': now(), 'requested_ids': pids[i:i + 1000], 'response': r.json()}
        save(file, obj)
    assert obj.get('account', 'oz_acc1') == args.account
    assert set(obj['requested_ids']) == {str(x['id']) for x in obj['response']['items']}
    items.update({str(x['id']): x for x in obj['response']['items']})
    print('Target product-info', i // 1000 + 1, 'cards', len(items), flush=True)

checked = []
for row in rows:
    x = items[row['product_id']]
    assert x['offer_id'] == row['offer_id']
    aliases = {str(s) for s in [x.get('sku')] + [v.get('sku') for v in x.get('sources', [])] if s}
    fail = []
    stock = x.get('stocks') or {}
    available = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stock.get('stocks') or []), D(0))
    if x.get('is_archived') or x.get('is_autoarchived'):
        fail.append('own_archived')
    if stock.get('has_stock') is not True or available <= 0:
        fail.append('own_no_available_stock')
    if not any(v.get('availability') == 'AVAILABLE' for v in x.get('availabilities') or []):
        fail.append('own_no_available_channel')
    if x.get('currency_code') != 'RUB' or D(str(x.get('price') or 0)) <= 0:
        fail.append('own_price_invalid')
    if x.get('errors'):
        fail.append('own_card_errors')
    if (x.get('visibility_details') or {}).get('has_price') is False:
        fail.append('own_price_hidden')
    if row['sku'] not in aliases:
        fail.append('sku_alias_identity_changed')
    if any(s in active for s in aliases):
        fail.append('already_in_other_running_CPC')
    checked.append({**row, 'fresh_check_at_utc': now(), 'fresh_available_stock_units': str(available),
                    'fresh_price_rub': str(x.get('price') or ''), 'fresh_sku_aliases': ';'.join(sorted(aliases)),
                    'fresh_model_id': str((x.get('model_info') or {}).get('model_id') or ''),
                    'eligible_before_floor': not fail, 'fresh_failures': ';'.join(fail)})
ids = [r['sku'] for r in checked if r['eligible_before_floor']]
floors = {}
for i in range(0, len(ids), 100):
    file = OUT / f'floor_batch_{i // 100:03}.json'
    if file.exists():
        z = json.loads(file.read_text())
        assert z['request']['sku'] == ids[i:i + 100]
    else:
        body = {'sku': ids[i:i + 100], 'paymentType': 'CPC', 'marketplaceId': 'MARKETPLACE_ID_RU'}
        for attempt in range(5):
            r = requests.post(ads.PERF + '/api/client/min/sku', headers=h, json=body, timeout=90)
            if r.status_code != 429:
                break
            time.sleep(3)
        r.raise_for_status()
        z = {'account': args.account, 'at_utc': now(), 'request': body, 'response': r.json()}
        save(file, z)
    assert z.get('account', 'oz_acc1') == args.account
    grouped = collections.defaultdict(list)
    for rec in z['response'].get('minBids') or []:
        grouped[str(rec['sku'])].append(rec)
    for sku in ids[i:i + 100]:
        rr = grouped[sku]
        if len(rr) == 1 and isinstance(rr[0].get('bid'), (int, float)) and not isinstance(rr[0]['bid'], bool):
            bid = D(str(rr[0]['bid']))
            if bid.is_finite() and bid > 0 and bid * 1000000 == (bid * 1000000).to_integral_value():
                floors[sku] = {'sku': sku, 'bid_rub': str(bid), 'bid_micro_rub': str(int(bid * 1000000)),
                               'checked_at_utc': z['at_utc'], 'source': file.name}
    if i % 1000 == 0:
        print('Individual floors', len(floors), '/', len(ids), flush=True)
    time.sleep(.15)
for row in checked:
    floor = floors.get(row['sku'])
    row['eligible_for_upload'] = row['eligible_before_floor'] and floor is not None
    row['bid_rub'] = floor['bid_rub'] if floor else ''
    row['bid_micro_rub'] = floor['bid_micro_rub'] if floor else ''
    if row['eligible_before_floor'] and not floor:
        row['fresh_failures'] = 'individual_floor_missing_or_invalid'
save(OUT / 'individual_floors.json', floors)
with (OUT / 'fresh_eligibility.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(checked[0])); w.writeheader(); w.writerows(checked)
save(OUT / 'preflight_summary.json', {'account': args.account, 'finished_at_utc': now(), 'fixed_pool': len(rows),
     'eligible_for_upload': sum(r['eligible_for_upload'] for r in checked), 'individual_floors': len(floors),
     'own_failures': dict(collections.Counter(reason for r in checked for reason in r['fresh_failures'].split(';') if reason)),
     'family_based_exclusions': 0, 'marketplace_mutations': 0})
print('PREFLIGHT COMPLETE', len(rows), 'eligible', sum(r['eligible_for_upload'] for r in checked), flush=True)
