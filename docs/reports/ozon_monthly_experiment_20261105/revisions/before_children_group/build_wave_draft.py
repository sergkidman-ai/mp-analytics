# поток: mkt / ozon
"""Draft three rollout waves by intact family blocks. No T/C assignment or marketplace changes."""
import collections
import csv
import datetime as dt
import hashlib
import json
import pathlib

OUT = pathlib.Path(__file__).parent
SOURCE = OUT.parent / 'ozon_full_inventory_20261105'
SEED = 'oz_acc1_monthly_rollout_20261105_DRAFT_V1'


def read(path):
    with path.open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def digest(value):
    return hashlib.sha256((SEED + ':' + value).encode()).hexdigest()


rows = read(SOURCE / 'in_stock/all_outside_cpc.csv')
assert len(rows) == 9242 and all(r['model_id'] for r in rows)
family_rows = collections.defaultdict(list)
alias_model = {}
for r in read(SOURCE / 'all_current_catalog.csv') + read(SOURCE / 'archived_outside_cpc.csv'):
    for s in r['sku_aliases'].split(';'):
        alias_model[s] = r['model_id']
for r in rows:
    family_rows[r['model_id']].append(r)
active_skus = json.loads((SOURCE / 'current_active_cpc.json').read_text())['skus']
active_models = {alias_model[s] for s in active_skus if s in alias_model}
sales = read(OUT.parent / 'ozon_recovery_20261103/revised_sales_lines.csv')
family_order_ids = collections.defaultdict(set)
for r in sales:
    if '2026-10-02' <= r['date_msk'] <= '2026-10-29':
        model = alias_model.get(r['sku'], r.get('current_model_id'))
        family_order_ids[model].add(r['order_id'])
blocks = []
for model, rr in family_rows.items():
    n_orders = len(family_order_ids[model])
    tier = 'no_orders' if n_orders == 0 else '1_2_orders' if n_orders <= 2 else '3plus_orders'
    segment_counts = collections.Counter(r['segment'] for r in rr)
    stratum = '|'.join([str(int(model in active_models)), tier, ','.join(sorted(segment_counts))])
    blocks.append({'model_id': model, 'skus': sorted(r['sku'] for r in rr), 'sku_count': len(rr),
                   'segments': dict(segment_counts), 'baseline_orders_28d': n_orders,
                   'baseline_dates': ['2026-10-02','2026-10-29'],
                   'known_existing_CPC_relative': model in active_models,
                   'stratum': stratum, 'tie_hash': digest(model)})

# The three-wave scenario includes the full pool. A persistent control is an explicit pending alternative.
loads = [0, 0, 0]
segment_loads = [collections.Counter() for _ in range(3)]
stratum_loads = [collections.Counter() for _ in range(3)]
assignment = {}
for block in sorted(blocks, key=lambda b: (-b['sku_count'], b['tie_hash'])):
    def score(w):
        global_cost = (loads[w] + block['sku_count']) ** 2
        mix_cost = sum((segment_loads[w][s] + n) ** 2 - segment_loads[w][s] ** 2 for s,n in block['segments'].items())
        stratum_cost = (stratum_loads[w][block['stratum']] + block['sku_count']) ** 2
        return (global_cost * 10 + mix_cost + stratum_cost * 2, digest(block['model_id'] + ':' + str(w)))
    wave = min(range(3), key=score)
    assignment[block['model_id']] = wave + 1
    loads[wave] += block['sku_count']
    segment_loads[wave].update(block['segments'])
    stratum_loads[wave][block['stratum']] += block['sku_count']

columns = ['sku','offer_id','name','model_id','segment','available_stock_units',
           'own_readiness_checks_passed','own_readiness_failures','draft_wave','launch_offset_days',
           'family_key','draft_status','treatment_control_assignment','actual_activation_at']
wave_rows = []
for r in rows:
    w = assignment[r['model_id']]
    wave_rows.append({**{k:r[k] for k in columns if k in r}, 'draft_wave':w,
                      'launch_offset_days':(w-1)*7, 'family_key':'oz_acc1:model_id:'+r['model_id'],
                      'draft_status':'DRAFT_NOT_APPLIED_CONTROL_OPTION_PENDING',
                      'treatment_control_assignment':'NOT_CREATED', 'actual_activation_at':''})
with (OUT / 'wave_draft_all_candidates.csv').open('w',encoding='utf-8-sig',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(wave_rows)
for w in [1,2,3]:
    with (OUT / f'wave_{w}_DRAFT.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns);writer.writeheader();writer.writerows(r for r in wave_rows if r['draft_wave']==w)

summary=[]
for w in [1,2,3]:
    rr=[r for r in wave_rows if r['draft_wave']==w]
    modelset={r['model_id'] for r in rr}
    summary.append({'wave':w,'offset_days':(w-1)*7,'planned_skus':len(rr),'family_blocks':len(modelset),
                    'segments':dict(collections.Counter(r['segment'] for r in rr)),
                    'own_basic_checks_passed_at_snapshot':sum(r['own_readiness_checks_passed']=='True' for r in rr),
                    'family_blocks_with_existing_CPC_relatives':sum(m in active_models for m in modelset)})
assert sum(r['planned_skus'] for r in summary)==9242
assert all(len({r['draft_wave'] for r in wave_rows if r['model_id']==m})==1 for m in family_rows)
family_summary={'family_blocks':len(blocks), 'family_blocks_with_existing_CPC_relatives':sum(b['known_existing_CPC_relative'] for b in blocks),
                'families_with_no_observed_orders_in_reference_28days':sum(b['baseline_orders_28d']==0 for b in blocks),
                'families_with_1_2_orders':sum(0<b['baseline_orders_28d']<=2 for b in blocks),
                'families_with_3plus_orders':sum(b['baseline_orders_28d']>=3 for b in blocks),
                'reference_baseline_is_not_final_prelaunch_window':True}
manifest={'account':'oz_acc1','created_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
          'status':'DRAFT_NOT_APPLIED','request':'3 weekly rollout waves, 1 month measurement, individual minimum CPC',
          'source_path':str((SOURCE/'in_stock/all_outside_cpc.csv').resolve()),
          'source_sha256':hashlib.sha256((SOURCE/'in_stock/all_outside_cpc.csv').read_bytes()).hexdigest(),
          'rollout_seed':SEED,'seed_is_proposed_not_an_approved_experiment_seed':True,
          'full_pool_scenario_skus':9242,'wave_summary':summary,'family_summary':family_summary,
          'actual_start_at':None,'actual_end_at':None,'one_month_interpretation':'PENDING_USER_CHOICE',
          'persistent_control_option':'PENDING_USER_CHOICE; no T/C created',
          'maximum_actual_incremental_CPC_spend_rub':None,
          'individual_floor_refresh_required_per_wave':True,'floor_bids_invented':False,
          'old_8Nov_deadline_not_used_for_this_new_monthly_protocol':True,
          'historic_E5_E8_assignments_modified':False,'marketplace_mutations':0,'database_data_writes':0}
(OUT/'wave_draft_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print('DRAFT waves:',summary)
print('Family measurement readiness:',family_summary)
print('No T/C created; no campaigns/bids/budgets changed. Three-wave CSVs are review drafts only.')
