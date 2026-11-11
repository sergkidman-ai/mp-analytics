# поток: mkt / WB read-only audit
"""Current nontrash cards, official FBS/FBO stock, disjoint segments and cautious bid trials."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
from core import db
from collectors import wb
from collectors.launch_cards import classify

P = Path(__file__).parent
load = lambda file: json.loads(file.read_text())
save = lambda file, z: file.write_text(json.dumps(z, ensure_ascii=False, indent=2, default=str) + '\n')
cards = load(P / 'cards_index.json')['cards']
assert len(cards) == len({int(c['nmID']) for c in cards})
cardmap = {int(c['nmID']): c for c in cards}
ads = load(P / 'per_nm_ads_orders.json')['cards']
details = load(P / 'current_campaigns.json')['campaigns']
running, configured = set(), set()
for c in details:
    target = {int(x['nm_id']) for x in c['nm_settings']}
    configured |= target
    if c['status'] == 9:
        running |= target
fbs = collections.defaultdict(int)
requested, returned = set(), set()
wh_counts = collections.Counter()
for file in sorted(P.glob('fbs_*.json')):
    z = load(file)
    assert z['account'] == 'wb_acc1'
    req = set(z['requested_skus'])
    got = [str(x['sku']) for x in z['response']['stocks']]
    assert len(got) == len(set(got)) and set(got) <= req
    requested |= req
    returned |= set(got)
    wh_counts[z['warehouse_id']] += len(got)
    for x in z['response']['stocks']:
        fbs[str(x['sku'])] += max(0, int(x['amount']))
assert requested, 'Missing official FBS response'
fbo = collections.defaultdict(D)
damaged = {x['warehouse'] for x in db.query('SELECT warehouse FROM wb_damage_stock_warehouse')}
loss = collections.defaultdict(D)
for x in db.query("SELECT d.warehouse,i.nm_id,i.qty FROM wb_loss_document d JOIN wb_loss_item i ON i.document_id=d.id WHERE d.account=%s AND (d.header_date IS NULL OR d.header_date<=%s)", ('wb_acc1', dt.date(2026, 11, 9))):
    loss[x['warehouse'], int(x['nm_id'])] += D(str(x['qty']))
stock = load(P / 'fbo_stock.json')
assert stock['complete'] and stock['account'] == 'wb_acc1'
excluded = D(0)
for row in stock['response']:
    nm = int(row['nmId'])
    for wh in row['warehouses']:
        name = wh['warehouseName']
        if not name or name in wb._PSEUDO_WH or name in wb._IN_WAY_COL:
            continue
        q = max(D(0), D(str(wh['quantity'])))
        cut = q if name in damaged else min(q, max(D(0), loss[name, nm]))
        fbo[nm] += q - cut
        excluded += cut

cutoff = dt.datetime(2026, 5, 9, tzinfo=ZoneInfo('Europe/Moscow'))
inventory = []
for c in cards:
    nm = int(c['nmID']); offer = c['vendorCode']; title = c.get('title') or ''
    role = classify(offer, title, 'wb_acc1')
    bundle = role[2] if role and role[1] == 'bundle' else None
    normalized = re.sub(r'\.(\d+)(?=Z|[+-]\d\d:\d\d$|$)', lambda m: '.' + (m[1] + '000000')[:6], c.get('createdAt') or '')
    created = dt.datetime.fromisoformat(normalized.replace('Z', '+00:00')) if normalized else None
    if bundle:
        segment = 'bundles'
    elif re.match(r'^[0-9]{5}', offer):
        segment = 'children'
    elif created is None:
        segment = 'unknown_age'
    else:
        segment = 'new_6months' if created >= cutoff else 'old'
    eans = {str(sku) for size in c.get('sizes') or [] for sku in size.get('skus') or []}
    fbs_q = sum(fbs[s] for s in eans)
    missing = eans - returned
    availability = 'IN_STOCK' if fbs_q > 0 or fbo[nm] > 0 else 'UNKNOWN' if missing or not eans else 'ZERO_STOCK'
    r = {'account': 'wb_acc1', 'nm_id': nm, 'vendor_code': offer, 'title': title, 'brand': c.get('brand'),
         'created_at': c.get('createdAt'), 'native_imt_id': c.get('imtID'), 'article_family': offer[:4],
         'segment': segment, 'bundle_quantity': bundle, 'in_running_ad': nm in running,
         'only_paused_or_ready_ad': nm not in running and nm in configured,
         'in_any_unfinished_campaign': nm in configured, 'availability': availability,
         'FBS_available_units': fbs_q, 'FBO_usable_units': str(fbo[nm]),
         'missing_FBS_barcodes': len(missing), 'outside_running_ad': nm not in running}
    inventory.append(r)

inv = {r['nm_id']: r for r in inventory}
orders = load(P / 'general_orders_raw.json')['orders']
latest = {}
for r in orders:
    key = str(r['srid'])
    if key not in latest or r['lastChangeDate'] > latest[key]['lastChangeDate']:
        latest[key] = r
general = [r for r in latest.values() if '2026-10-12' <= r['date'][:10] <= '2026-11-08' and not r['isCancel']]
model_orders = collections.defaultdict(set)
family_orders = collections.defaultdict(set)
unknown_sold = set()
for r in general:
    nm = int(r['nmId'])
    if nm not in cardmap:
        unknown_sold.add(nm)
        article = str(r.get('supplierArticle') or '')
        if re.match(r'^[0-9]{4}', article):
            family_orders[article[:4]].add(str(r['srid']))
        continue
    imt = cardmap[nm].get('imtID')
    if imt:
        model_orders[str(imt)].add(str(r['srid']))
    family_orders[cardmap[nm]['vendorCode'][:4]].add(str(r['srid']))
floors = {}
for file in P.glob('min_bids_*.json'):
    z = load(file)
    assert z['account'] == 'wb_acc1' and z['http'] == 200
    for r in z['response']['bids']:
        bids = [b for b in r['bids'] if b['type'] == 'search']
        assert len(bids) == 1 and bids[0]['currency'] == 'RUB'
        floors[z['request']['advert_id'], int(r['nm_id'])] = int(bids[0]['value'])
review = load(P / 'cost_efficiency_review_pool.json')
proposals, already_floor, stock_excluded = [], [], []
for r in review:
    nm = int(r['nm_id'])
    if nm not in inv or inv[nm]['availability'] != 'IN_STOCK':
        stock_excluded.append(nm)
        continue
    for place in r['current_placements']:
        if place['campaign_state'] != 9:
            continue
        cid = place['campaign_id']; old = int(place['search_bid_kopecks'])
        minimum = floors.get((cid, nm))
        assert minimum is not None, 'Unverified native minimum cannot produce a bid proposal'
        new = max(minimum, int((D(old) * D('.9')).quantize(D(1), rounding=ROUND_HALF_UP)))
        row = {**{k: inv[nm][k] for k in ['account', 'nm_id', 'vendor_code', 'title', 'segment', 'native_imt_id', 'article_family', 'FBS_available_units', 'FBO_usable_units']},
               'campaign_id': cid, 'payment_type': place['payment_type'], 'bid_type': place['bid_type'],
               'current_bid_rub': str(D(old) / 100), 'native_min_rub': str(D(minimum) / 100),
               'proposed_bid_rub': str(D(new) / 100), 'automatic_application_allowed': False,
               'spend_28d': r['spend_28d'], 'ad_orders_28d': r['ad_orders_28d'], 'ad_revenue_28d': r['ad_revenue_28d'],
               'ad_drr_28d': r['ad_drr_28d'], 'general_orders_28d': r['general_orders_28d'],
               'general_ordered_value_28d': r['general_ordered_value_28d'], 'clicks_28d': r['clicks_28d'],
               'known_native_model_orders_28d': len(model_orders[str(inv[nm]['native_imt_id'])]),
               'known_article_family_orders_28d': len(family_orders[inv[nm]['article_family']]),
               'spend_7d': r['spend_7d'], 'ad_orders_7d': r['ad_orders_7d'], 'general_orders_7d': r['general_orders_7d'],
               'spend_prior7d': r['spend_prior7d'], 'general_orders_prior7d': r['general_orders_prior7d'],
               'live_margin_available': False, 'causal_redundancy_proved': False}
        if new >= old:
            row['reason'] = 'ALREADY_AT_OR_BELOW_NATIVE_FLOOR'
            already_floor.append(row)
        else:
            row['reason'] = 'ZERO_AD_ORDERS_OBSERVATIONAL_TRIAL' if D(r['ad_orders_28d']) == 0 else 'HIGH_AD_DRR_SMALL_SAMPLE_REVIEW'
            row['priority'] = 'FIRST_TRIAL' if (D(r['ad_orders_28d']) == 0 and D(r['spend_28d']) >= 250
                and D(r['clicks_28d']) >= 20 and D(r['spend_7d']) >= 50) else 'SECONDARY_HALO_REVIEW' if (
                row['known_native_model_orders_28d'] > int(r['general_orders_28d'])) else 'SECONDARY_LOW_SAMPLE'
            proposals.append(row)
proposals.sort(key=lambda r: (r['priority'] != 'FIRST_TRIAL', -D(r['spend_28d'])))


def output(name, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with (P / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)


output('inventory_all_nontrash.csv', inventory)
in_stock = [r for r in inventory if r['availability'] == 'IN_STOCK']
outside = [r for r in in_stock if not r['in_running_ad']]
output('outside_running_ads_in_stock.csv', outside)
output('outside_all_unfinished_ads_in_stock.csv', [r for r in in_stock if not r['in_any_unfinished_campaign']])
for segment in ['bundles', 'children', 'new_6months', 'old']:
    output('outside_' + segment + '.csv', [r for r in outside if r['segment'] == segment])
output('bid_candidates.csv', proposals); output('already_at_floor.csv', already_floor)
summary = {'account': 'wb_acc1', 'asof_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
           'nontrash_catalog_total': len(inventory), 'current_running_campaigns': sum(c['status'] == 9 for c in details),
           'current_paused_campaigns': sum(c['status'] == 11 for c in details), 'current_CPC_manual_campaigns': len(details),
           'raw_current_running_membership_unique': len(running), 'running_members_absent_current_catalog': len(running - set(inv)),
           'catalog_in_running_ads': sum(r['in_running_ad'] for r in inventory),
           'stock_availability_counts': dict(collections.Counter(r['availability'] for r in inventory)),
           'in_stock_total': len(in_stock), 'in_stock_running_ads': sum(r['in_running_ad'] for r in in_stock),
           'in_stock_outside_running_ads': len(outside), 'in_stock_paused_only': sum(r['only_paused_or_ready_ad'] for r in outside),
           'in_stock_no_unfinished_campaign': sum(not r['in_any_unfinished_campaign'] for r in in_stock),
           'segments_outside_running_in_stock': dict(collections.Counter(r['segment'] for r in outside)),
           'segments_in_running_in_stock': dict(collections.Counter(r['segment'] for r in in_stock if r['in_running_ad'])),
           'bundle_quantities_outside_running_in_stock': dict(collections.Counter(r['bundle_quantity'] for r in outside if r['segment'] == 'bundles')),
           'new_cutoff_msk': cutoff.isoformat(), 'new_definition': 'WB createdAt; bundles and numeric-prefix children take priority; not first publication',
           'FBS_requested_barcodes': len(requested), 'FBS_returned_barcodes': len(returned), 'FBS_missing_not_zero': len(requested - returned),
           'FBO_excluded_damage_units': str(excluded), 'public_buyer_visible_stock_status': 'HTTP403_NOT_CONFIRMED; official FBS/FBO availability used',
           'unknown_sold_archived_or_absent_NM_count': len(unknown_sold), 'unknown_sold_NMs': sorted(unknown_sold),
           'bid_candidates_above_verified_floor': len(proposals), 'first_trial_candidates': sum(r['priority'] == 'FIRST_TRIAL' for r in proposals),
           'review_pool_already_at_floor': len(already_floor), 'review_pool_stock_excluded': stock_excluded,
           'live_margin_available': False, 'marketplace_mutations': 0, 'database_writes': 0}
tail = []
for r in in_stock:
    a = ads.get(str(r['nm_id']))
    if not r['in_running_ad'] or not a or D(a['spend_28d']) <= 0:
        continue
    if D(a['ad_orders_28d']) == 0 and a['general_orders_28d'] == 0:
        tail.append({'account': 'wb_acc1', 'nm_id': r['nm_id'], 'vendor_code': r['vendor_code'],
                     'spend_28d': a['spend_28d'], 'ad_orders_28d': 0, 'general_orders_28d': 0,
                     'known_native_model_orders': len(model_orders[str(r['native_imt_id'])]),
                     'article_family_orders': len(family_orders[r['article_family']]),
                     'current_search_bids_rub': ';'.join(str(D(x['search_bid_kopecks']) / 100)
                         for x in a['current_placements'] if x['campaign_state'] == 9),
                     'floor_verified_for_this_card': any((x['campaign_id'], r['nm_id']) in floors
                         for x in a['current_placements'] if x['campaign_state'] == 9)})
output('paid_no_own_or_ad_orders.csv', tail)
summary['paid_no_own_or_ad_orders'] = {'cards': len(tail), 'spend_28d': str(sum((D(x['spend_28d']) for x in tail), D(0))),
    'without_known_native_or_article_orders_cards': sum(x['known_native_model_orders'] == 0 and x['article_family_orders'] == 0 for x in tail),
    'without_known_native_or_article_orders_spend_28d': str(sum((D(x['spend_28d']) for x in tail if x['known_native_model_orders'] == 0 and x['article_family_orders'] == 0), D(0))),
    'causal_waste_proved': False, 'no_automatic_pause_authorized': True}
assert sum(summary['segments_outside_running_in_stock'].values()) == len(outside)
assert summary['in_stock_running_ads'] + len(outside) == len(in_stock)
save(P / 'summary.json', summary)
print(json.dumps(summary, ensure_ascii=False))
for r in proposals:
    print(r['priority'], r['vendor_code'], r['current_bid_rub'], '->', r['proposed_bid_rub'],
          'spend', r['spend_28d'], 'adOrders', r['ad_orders_28d'], 'own', r['general_orders_28d'], 'native', r['known_native_model_orders_28d'])
