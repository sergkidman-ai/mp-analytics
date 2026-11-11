# поток: mkt / ozon
"""Offline partition of a captured full catalog. No marketplace or database writes."""
import collections
import csv
import datetime as dt
import decimal
import itertools
import json
import pathlib
import re
from zoneinfo import ZoneInfo

OUT = pathlib.Path(__file__).parent
RAW = OUT / 'raw'
TZ = ZoneInfo('Europe/Moscow')
D = decimal.Decimal


def load(path):
    return json.loads(path.read_text())


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def write(name, rows, fields):
    with (OUT / name).open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def timestamp(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


acquisition = load(OUT / 'acquisition_summary.json')
prepared_path = OUT.parent / 'ozon_recovery_20261103/revised_prepared_composition_2953.csv'
with prepared_path.open() as f:
    prepared = list(csv.DictReader(f))
previous_wave = {r['sku']: r['stream'] for r in prepared}
asof = timestamp(acquisition['finished_at_utc'])
cutoff = dt.datetime(2026, 5, 5, tzinfo=TZ)
items = {}
sources = {}
info_coverage = []
for f in sorted(RAW.glob('info_*.json')):
    z = load(f)
    requested = {str(x) for x in z['body']['product_id']}
    returned = {str(x['id']) for x in z['response'].get('items', [])}
    info_coverage.append({'source': str(f.relative_to(OUT)), 'http': z['http'],
                          'requested': len(requested), 'returned': len(returned),
                          'missing_product_ids': sorted(requested - returned)})
    for x in z['response'].get('items', []):
        pid = str(x['id'])
        if pid not in sources or z['at_utc'] > sources[pid]['at_utc']:
            items[pid] = x
            sources[pid] = {'path': str(f.relative_to(OUT)), 'at_utc': z['at_utc']}
listed = set(map(str, load(OUT / 'product_ids_all.json'))) | set(map(str, load(OUT / 'product_ids_archived.json')))
assert listed == set(items), 'Full product-info coverage is required'
assert all(x['http'] == 200 and not x['missing_product_ids'] for x in info_coverage)

aliases = {}
alias_conflicts = []
product_aliases = {}
for pid, x in items.items():
    ids = {str(s) for s in [x.get('sku')] + [v.get('sku') for v in x.get('sources', [])] if s}
    product_aliases[pid] = ids
    for s in ids:
        if s in aliases and aliases[s] != pid:
            alias_conflicts.append({'sku': s, 'product_a': aliases[s], 'product_b': pid})
        aliases[s] = pid
assert not alias_conflicts, 'SKU alias belongs to multiple product cards'

campaigns = load(OUT / 'current_campaigns.json')['campaigns']
all_cpo_running = any(c['state'] == 'CAMPAIGN_STATE_RUNNING' and c.get('advObjectType') == 'ALL_SKU_PROMO' for c in campaigns)
cpc = collections.defaultdict(set)
for f in OUT.glob('active_products_*.json'):
    z = load(f)
    for x in z['products']:
        cpc[str(x['sku'])].add(z['campaign_id'])
promo = {}
for r in load(OUT / 'current_search_promo.json')['products']:
    for s in [r.get('sku'), r.get('sourceSku')]:
        if s:
            promo.setdefault(str(s), []).append(r)

rows = []
for pid, x in items.items():
    sku = str(x.get('sku') or '')
    aliases_for_card = product_aliases[pid]
    active = sorted(set().union(*(cpc.get(s, set()) for s in aliases_for_card)))
    promo_rows = [r for s in aliases_for_card for r in promo.get(s, [])]
    promo_enabled = any(r.get('searchPromoStatus') is True for r in promo_rows)
    offer = x.get('offer_id', '')
    bundle = re.fullmatch(r'(.+)X(2|4|6|8|10)', offer)
    bundle_n = int(bundle.group(2)) if bundle else 1
    created = timestamp(x.get('created_at'))
    young = created is not None and cutoff <= created <= asof
    if bundle:
        segment = 'bundles_x' + str(bundle_n)
    elif created is None:
        segment = 'unknown_creation_date'
    elif young:
        segment = 'new_6months'
    else:
        segment = 'other_outside_cpc'
    archived = bool(x.get('is_archived') or x.get('is_autoarchived'))
    stock = bool((x.get('stocks') or {}).get('has_stock'))
    available = any(v.get('availability') == 'AVAILABLE' for v in x.get('availabilities', []))
    currency = x.get('currency_code')
    try:
        positive_price = D(str(x.get('price') or '0')) > 0
    except decimal.InvalidOperation:
        positive_price = False
    failures = []
    if archived: failures.append('archived_or_autoarchived')
    if not stock: failures.append('no_own_stock')
    if not available: failures.append('no_available_channel')
    if currency != 'RUB' or not positive_price: failures.append('own_price_not_positive_RUB')
    if x.get('errors'): failures.append('own_card_errors')
    if (x.get('visibility_details') or {}).get('has_price') is False:
        failures.append('own_price_not_visible')
    if not sku: failures.append('missing_primary_sku')
    if active: failures.append('already_in_running_CPC')
    rows.append({'account': 'oz_acc1', 'sku': sku, 'product_id': pid, 'offer_id': offer,
                 'name': x.get('name', ''), 'sku_aliases': ';'.join(sorted(aliases_for_card)),
                 'created_at_utc': x.get('created_at', ''),
                 'created_at_msk': created.astimezone(TZ).isoformat() if created else '',
                 'created_within_6months': young, 'segment': segment, 'bundle_count': bundle_n,
                 'bundle_base_offer_id': bundle.group(1) if bundle else '',
                 'bundle_evidence': 'exact_offer_id_suffix_X' + str(bundle_n) if bundle else '',
                 'model_id': str((x.get('model_info') or {}).get('model_id') or ''),
                 'archived': archived, 'running_cpc_campaign_ids': ';'.join(active),
                 'outside_running_cpc': not active, 'individual_cpo_enabled': promo_enabled,
                 'individual_cpo_pool_present': bool(promo_rows),
                 'global_all_sku_cpo_running': all_cpo_running,
                 'global_cpo_applies_to_current_own_stock': all_cpo_running and stock and not archived,
                 'own_has_stock': stock, 'own_available_channel': available,
                 'own_price_rub': str(x.get('price') or ''), 'currency': currency,
                 'own_card_errors_count': len(x.get('errors') or []),
                 'own_readiness_checks_passed': not failures,
                 'own_readiness_failures': ';'.join(failures),
                 'new_campaign_floor_refreshed': False,
                 'metadata_snapshot_utc': sources[pid]['at_utc'], 'metadata_source': sources[pid]['path']})
    rows[-1].update(previous_wave1_prepared=sku in previous_wave,
                    previous_wave1_stream=previous_wave.get(sku, ''),
                    previous_bundle_label_without_X=previous_wave.get(sku) == 'bundles' and bundle is None)

fields = list(rows[0])
live = [r for r in rows if not r['archived']]
outside = [r for r in live if r['outside_running_cpc']]
archived_outside = [r for r in rows if r['archived'] and r['outside_running_cpc']]
groups = {name: [r for r in outside if r['segment'] == name] for name in
          ['bundles_x2', 'bundles_x6', 'bundles_x10', 'bundles_x4', 'bundles_x8',
           'new_6months', 'other_outside_cpc', 'unknown_creation_date']}
for name, rr in groups.items():
    write(name + '.csv', rr, fields)
write('all_outside_cpc.csv', outside, fields)
write('all_current_catalog.csv', live, fields)
write('archived_outside_cpc.csv', archived_outside, fields)
write('all_outside_cpc_including_archive.csv', outside + archived_outside, fields)
write('bundles_all.csv', [r for r in outside if r['bundle_count'] > 1], fields)
write('currently_ready_outside_cpc.csv', [r for r in outside if r['own_readiness_checks_passed']], fields)
write('previous_bundle_label_without_marker.csv', [r for r in outside if r['previous_bundle_label_without_X']], fields)

overlaps = []
for (a, ra), (b, rb) in itertools.combinations(groups.items(), 2):
    overlap = {r['product_id'] for r in ra} & {r['product_id'] for r in rb}
    overlaps.append({'segment_a': a, 'segment_b': b, 'overlap_product_ids': len(overlap)})
assert not any(r['overlap_product_ids'] for r in overlaps)
assert sum(len(rr) for rr in groups.values()) == len(outside)
assert len({r['sku'] for r in outside}) == len(outside), 'Primary SKU duplication'
assert len({r['offer_id'] for r in outside}) == len(outside), 'Offer duplication'
counts = {name: {'count': len(rr), 'own_readiness_passed': sum(r['own_readiness_checks_passed'] for r in rr),
                 'created_within_6months': sum(r['created_within_6months'] for r in rr),
                 'no_own_stock': sum(not r['own_has_stock'] for r in rr),
                 'individual_cpo_enabled': sum(r['individual_cpo_enabled'] for r in rr),
                 'global_cpo_applies_to_current_own_stock': sum(r['global_cpo_applies_to_current_own_stock'] for r in rr)} for name, rr in groups.items()}
outside_by_sku = {r['sku']: r for r in outside}
recovery = collections.Counter()
for r in prepared:
    current = outside_by_sku.get(r['sku'])
    recovery[(r['stream'], current['segment'] if current else 'not_in_current_primary_outside_cpc')] += 1

summary = {'account': 'oz_acc1', 'snapshot_finished_at_utc': acquisition['finished_at_utc'],
           'new_cards_window_from_msk': cutoff.isoformat(), 'new_cards_window_through_utc': asof.isoformat(),
           'date_basis': 'Seller product created_at, not updated_at or source SKU creation time',
           'catalog_product_ids': len(rows), 'nonarchived_catalog_cards': len(live),
           'nonarchived_in_running_cpc': len(live) - len(outside),
           'nonarchived_outside_running_cpc': len(outside), 'archived_outside_cpc': len(archived_outside),
           'global_all_sku_cpo_running': all_cpo_running,
           'individual_cpo_enabled_in_catalog': sum(r['individual_cpo_enabled'] for r in live),
           'outside_cpc_with_global_cpo_current_stock': sum(r['global_cpo_applies_to_current_own_stock'] for r in outside),
           'outside_cpc_own_readiness_passed': sum(r['own_readiness_checks_passed'] for r in outside),
           'counts': counts, 'priority': ['bundles', 'new_6months_nonbundles', 'remaining_older_nonbundles'],
           'young_bundles_retained_in_bundle_group': sum(r['created_within_6months'] for r in outside if r['bundle_count'] > 1),
           'pairwise_overlap_product_ids': 0, 'sku_alias_conflicts': 0,
           'all_product_info_coverage_complete': True,
           'outside_no_paid_advertising_claimed': False,
           'scope_caveat': 'Lists exclude running CPC, not the account-wide ALL_SKU_PROMO. Global CPO is running; individual searchPromoStatus=false does not disable it.',
           'current_readiness_is_not_launch_permission': True,
           'previous_first_wave_crosswalk': [{'old_stream': a, 'current_segment': b, 'count': n} for (a,b),n in sorted(recovery.items())],
           'marketplace_mutations': 0, 'database_data_writes': 0}
save('summary.json', summary)
save('validation.json', {'product_info_coverage': info_coverage, 'overlap_checks': overlaps,
                         'unique_primary_skus': len({r['sku'] for r in outside}),
                         'unique_offer_ids': len({r['offer_id'] for r in outside}),
                         'partition_union_count': sum(len(rr) for rr in groups.values()),
                         'outside_count': len(outside), 'alias_conflicts': alias_conflicts})
print('Current catalog:', len(live), 'outside RUNNING CPC:', len(outside), 'archive outside:', len(archived_outside))
print('Exclusive segments:', {name: value['count'] for name, value in counts.items()})
print('Own readiness passed:', summary['outside_cpc_own_readiness_passed'], 'global CPO stock applies:', summary['outside_cpc_with_global_cpo_current_stock'])
print('Pairwise overlap checks:', len(overlaps), 'all zero; primary SKU/offer IDs unique; full product-info coverage PASS')
