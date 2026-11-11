# поток: mkt / ozon
"""Offline V3 plan: bundle-size waves, intact article families, campaign previews."""
import collections
import csv
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

OUT = Path(__file__).parent
BASE = OUT.parent
SOURCE = BASE / 'ozon_full_inventory_20261105/in_stock/all_outside_cpc.csv'
SEED = 'oz_acc1_bundle_wave_rule_20261106_DRAFT_V3'
URL = '/api/wb-bids/reports/ozon-waves-2026-11-06'
LABELS = {'new_6months': 'Новинки', 'other_outside_cpc': 'Старые', 'children': 'Дочерние',
          'bundles_x2': 'Бандлы X2', 'bundles_x6': 'Бандлы X6', 'bundles_x10': 'Бандлы X10'}
FORCED = {'bundles_x2': 1, 'bundles_x6': 2, 'bundles_x10': 3}


def read(path):
    with path.open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def write(name, data):
    if not data:
        return
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)


def tie(key):
    return hashlib.sha256((SEED + ':' + key).encode()).hexdigest()


rows = read(SOURCE)
assert len(rows) == 9242
previous = read(BASE / 'ozon_monthly_experiment_20261105/wave_draft_all_candidates.csv')
previous_wave = {r['sku']: int(r['draft_wave']) for r in previous}
assert set(previous_wave) == {r['sku'] for r in rows}

# Children share a wave by article-parent relation and Ozon model_id.
# Parents are NOT added or required in the children's wave (owner's latest clarification).
# The owner's explicit X2/X6/X10 rule necessarily splits some bundle model_ids.
normal = [r for r in rows if r['segment'] not in FORCED]
roots = {r['sku']: r['sku'] for r in normal}


def find(s):
    while roots[s] != s:
        roots[s] = roots[roots[s]]
        s = roots[s]
    return s


def union(a, b):
    roots[find(b)] = find(a)


seen_article = {}
seen_model = {}
for r in normal:
    article_key = r['parent_offer_id'] if r['segment'] == 'children' else ''
    for key, index in [(article_key, seen_article), (r['model_id'], seen_model)]:
        if key:
            if key in index:
                union(r['sku'], index[key])
            else:
                index[key] = r['sku']
units = collections.defaultdict(list)
for r in normal:
    units[find(r['sku'])].append(r)
totals = collections.Counter(r['segment'] for r in normal)
loads = [0, 0, 0]
mix = [collections.Counter() for _ in range(3)]
assignment = {r['sku']: FORCED[r['segment']] for r in rows if r['segment'] in FORCED}
for rr in sorted(units.values(), key=lambda x: (-len(x), tie(min(r['sku'] for r in x)))):
    counts = collections.Counter(r['segment'] for r in rr)
    key = min(r['sku'] for r in rr)

    def score(w):
        segment_cost = sum(((mix[w][s] + n) ** 2 - mix[w][s] ** 2) / (totals[s] / 3) ** 2
                           for s, n in counts.items())
        total_cost = ((loads[w] + len(rr)) ** 2 - loads[w] ** 2) / (len(normal) / 3) ** 2
        return segment_cost + total_cost, tie(key + ':' + str(w))

    w = min(range(3), key=score)
    for r in rr:
        assignment[r['sku']] = w + 1
    loads[w] += len(rr)
    mix[w].update(counts)

draft = []
for r in rows:
    w = assignment[r['sku']]
    title = f"oz_acc1 | Волна {w} | {LABELS[r['segment']]} | MIN CPC | 20261106"
    draft.append({**r, 'draft_wave': w, 'previous_v2_wave': previous_wave[r['sku']],
                  'launch_offset_days': (w - 1) * 7, 'campaign_title': title,
                  'campaign_key': f"wave{w}_{r['segment']}", 'actual_campaign_id': '',
                  'individual_floor_rub': '', 'actual_activation_at': '',
                  'treatment_control_assignment': 'NOT_CREATED',
                  'draft_status': 'DRAFT_NOT_APPLIED'})
write('wave_draft_all_candidates.csv', draft)
for w in [1, 2, 3]:
    write(f'wave_{w}_DRAFT.csv', [r for r in draft if r['draft_wave'] == w])

existing = json.loads((OUT / 'campaigns_current.json').read_text())
current_titles = {c['title'] for c in existing['response']['list']}
campaigns = []
for w in [1, 2, 3]:
    for segment in ['new_6months', next(s for s, sw in FORCED.items() if sw == w),
                    'other_outside_cpc', 'children']:
        rr = [r for r in draft if r['draft_wave'] == w and r['segment'] == segment]
        title = rr[0]['campaign_title']
        assert title not in current_titles, 'Existing title requires reconciliation, never duplicate creation'
        key = rr[0]['campaign_key']
        write(f'campaign_{key}.csv', rr)
        campaigns.append({'key': key, 'wave': w, 'segment': segment, 'title': title,
                          'planned_skus': len(rr),
                          'own_checks_passed_at_5Nov_snapshot': sum(r['own_readiness_checks_passed'] == 'True' for r in rr),
                          'actual_campaign_id': None, 'actual_creation_at': None,
                          'actual_activation_at': None, 'status': 'PREVIEW_ONLY_NOT_CREATED',
                          'native_weekly_minimum_preview_rub': len(rr) * 2000,
                          'native_budget_is_spend_forecast': False,
                          'approved_weekly_budget_rub': None,
                          'create_payload_preview': {'title': title, 'fromDate': None, 'toDate': None,
                              'placement': 'PLACEMENT_SEARCH_AND_CATEGORY',
                              'productCampaignMode': 'PRODUCT_CAMPAIGN_MODE_AUTO',
                              'productAutopilotStrategy': 'TARGET_BIDS', 'PaymentType': 'CPC',
                              'expenseStrategy': 'DAILY_BUDGET',
                              'budgetType': 'PRODUCT_CAMPAIGN_BUDGET_TYPE_WEEKLY',
                              'weeklyBudget': None, 'autoIncreasePercent': 0},
                          'sku_file': f'campaign_{key}.csv',
                          'individual_floors_must_be_refreshed': True})
write('campaign_plan.csv', [{k: c[k] for k in ['key', 'wave', 'segment', 'title', 'planned_skus',
                  'own_checks_passed_at_5Nov_snapshot', 'actual_campaign_id', 'status',
                  'native_weekly_minimum_preview_rub', 'approved_weekly_budget_rub']} for c in campaigns])

models = collections.defaultdict(list)
children = collections.defaultdict(list)
for r in draft:
    models[r['model_id']].append(r)
    if r['segment'] == 'children':
        children[r['parent_offer_id']].append(r)
forced_conflicts = [rr for rr in models.values()
                    if len({FORCED[r['segment']] for r in rr if r['segment'] in FORCED}) > 1]
model_splits = [rr for rr in models.values() if len({r['draft_wave'] for r in rr}) > 1]
write('model_wave_context.csv', [{'model_id': mid, 'pool_skus': len(rr),
      'waves': ';'.join(map(str, sorted({r['draft_wave'] for r in rr}))),
      'segments': ';'.join(sorted({r['segment'] for r in rr})),
      'forced_bundle_size_split': len({FORCED[r['segment']] for r in rr if r['segment'] in FORCED}) > 1,
      'one_wave_per_model_claimed': len({r['draft_wave'] for r in rr}) == 1} for mid, rr in sorted(models.items())])

# Partial 14 September archive: evidence only, never a current rating or inherited stars.
archive = read(Path('data/mpstats_probe/2026-09-14/derived/calib_list_ozon.csv'))
historical = {r['marketplace_id']: r for r in archive if r.get('account') == 'oz_acc1'}
catalog = {r['sku']: r for r in read(BASE / 'ozon_full_inventory_20261105/all_current_catalog.csv')}
family_rows = []
rating_rows = []
for parent, rr in sorted(children.items()):
    first = rr[0]
    assert len({r['parent_product_id'] for r in rr}) == 1
    assert len({r['draft_wave'] for r in rr}) == 1
    h = historical.get(first['parent_sku'], {})
    parent_record = catalog.get(first['parent_sku'], {})
    family_rows.append({'parent_offer_id': parent, 'parent_product_id': first['parent_product_id'],
       'parent_sku': first['parent_sku'], 'parent_name': parent_record.get('name', ''),
       'eligible_child_skus_in_fixed_pool': len(rr), 'child_offer_ids': ';'.join(sorted(r['offer_id'] for r in rr)),
       'child_skus': ';'.join(sorted(r['sku'] for r in rr)), 'draft_wave': first['draft_wave'],
       'parent_in_same_fixed_pool': parent in {r['offer_id'] for r in draft},
       'parent_is_archived': first['parent_is_archived'], 'parent_has_stock': first['parent_has_stock'],
       'parent_available_stock_units': first['parent_available_stock_units'],
       'current_parent_rating': '', 'current_parent_rating_status': 'UNKNOWN',
       'current_rated_children_count': '', 'current_child_rating_status': 'UNKNOWN',
       'historical_parent_rating_20260914': h.get('mp_rating', ''),
       'historical_parent_reviews_20260914': h.get('mp_comments', '')})
    for r in rr:
        hs = next((historical[s] for s in r['sku_aliases'].split(';') if s in historical), {})
        rating_rows.append({'sku': r['sku'], 'offer_id': r['offer_id'], 'parent_offer_id': parent,
           'parent_sku': r['parent_sku'], 'current_rating': '', 'current_reviews': '',
           'current_rating_status': 'UNKNOWN_NOT_ZERO',
           'historical_rating_20260914': hs.get('mp_rating', ''),
           'historical_reviews_20260914': hs.get('mp_comments', ''),
           'historical_source': 'data/mpstats_probe/2026-09-14/derived/calib_list_ozon.csv' if hs else '',
           'parent_rating_inherited': False})
write('children_families.csv', family_rows)
write('children_rating_evidence.csv', rating_rows)
distribution = collections.Counter(len(rr) for rr in children.values())
write('children_family_sizes.csv', [{'eligible_daughters': n, 'families': count, 'daughter_skus': n * count}
                                  for n, count in sorted(distribution.items())])
family_summary = {'child_skus': sum(map(len, children.values())), 'one_parent_families': len(children),
   'single_eligible_child_families': distribution[1],
   'multiple_eligible_child_families': sum(count for n, count in distribution.items() if n >= 2),
   'max_eligible_children_per_family': max(distribution),
   'parent_plus_distinct_fixed_pool_children_cards': len(children) + sum(map(len, children.values())),
   'archived_parent_families': sum(r['parent_is_archived'] == 'True' for r in family_rows),
   'children_with_archived_parent': sum(r['parent_is_archived'] == 'True' for r in draft if r['segment'] == 'children'),
   'parents_also_in_fixed_pool': sum(r['parent_in_same_fixed_pool'] for r in family_rows),
   'family_size_distribution': dict(sorted(distribution.items())),
   'current_rated_children_count': None, 'current_rating_unknown_skus': 3252,
   'historical_children_matched_20260914': sum(bool(r['historical_source']) for r in rating_rows),
   'rating_data_is_current': False, 'family_definition': 'one exact 4-digit parent and fixed-pool numeric child offers sharing that prefix'}
save('children_family_summary.json', family_summary)

wave_summary = [{'wave': w, 'offset_days': (w - 1) * 7,
      'planned_skus': sum(r['draft_wave'] == w for r in draft),
      'segments': dict(collections.Counter(r['segment'] for r in draft if r['draft_wave'] == w)),
      'children_families': sum(r['draft_wave'] == w for r in family_rows)} for w in [1, 2, 3]]
manifest = {'account': 'oz_acc1', 'version': 'V3_BUNDLE_WAVES_20261106',
    'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
    'status': 'DRAFT_CAMPAIGNS_NOT_CREATED', 'source_path': str(SOURCE.resolve()),
    'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    'stock_snapshot': '2026-11-05T12:33:00+03:00; refresh own eligibility before creation',
    'rollout_seed': SEED, 'seed_is_draft_only': True,
    'explicit_owner_rule': {'wave1': 'X2 only among bundles', 'wave2': 'X6 only among bundles', 'wave3': 'X10 only among bundles'},
    'wave_summary': wave_summary, 'campaigns': campaigns, 'campaign_count': len(campaigns),
    'child_family_summary': family_summary, 'nonbundle_connected_units': len(units),
    'whole_model_id_wave_rule_superseded_by_explicit_bundle_instruction': True,
    'parents_added_to_pool': 0, 'parent_same_wave_required': False,
    'models_forced_to_multiple_waves_by_bundle_sizes': len(forced_conflicts),
    'model_ids_actually_in_multiple_waves': len(model_splits),
    'changed_sku_waves_vs_v2': sum(r['draft_wave'] != r['previous_v2_wave'] for r in draft),
    'T_C_assignments_created': 0, 'historic_E5_E8_modified': False,
    'actual_start_at': None, 'actual_end_at': None, 'one_month_interpretation': 'PENDING',
    'maximum_actual_incremental_CPC_spend_rub': None,
    'creation_blockers': ['weekly budgets and total expense cap not provided', 'stale own eligibility must be refreshed', 'individual minimum bids not refreshed'],
    'activation_blockers': ['creation/readback not performed', 'month boundary and persistent control unresolved', 'auto-stop not installed'],
    'marketplace_mutations': 0, 'database_writes': 0}
save('wave_draft_manifest.json', manifest)
save('cost_preview.json', {'per_sku_weekly_native_minimum_preview_rub': 2000,
     'rule_source': 'https://docs.ozon.kz/performance/product-ads/',
     'rule_source_scope': 'Ozon Performance help; exact account/API applicability must be confirmed before creation',
     'cumulative_weekly_native_minimum_preview_rub': [sum(s['planned_skus'] for s in wave_summary if s['wave'] <= w) * 2000 for w in [1, 2, 3]],
     'approved_actual_spend_cap_rub': None, 'actual_expense_forecast': None,
     'not_actual_spending_and_not_approved_budget': True})
assert len(campaigns) == 12 and sum(c['planned_skus'] for c in campaigns) == 9242
assert len(children) == 1043 and sum(map(len, children.values())) == 3252
for r in draft:
    if r['segment'] in FORCED:
        assert r['draft_wave'] == FORCED[r['segment']]
save('validation.json', {'same_9242_sku_pool': True, '12_disjoint_campaigns': True,
     'bundle_wave_rule_passed': True, 'all_1043_child_families_intact': True,
     'parents_not_added': True, 'parent_same_wave_required': False, 'rating_unknown_not_zero': True,
     'actual_campaigns_created': 0, 'marketplace_mutations': 0})
print('V3 waves:', wave_summary)
print('Campaign previews:', len(campaigns), 'child families:', len(children), 'forced model splits:', len(forced_conflicts))
