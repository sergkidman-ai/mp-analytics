# поток: mkt / ozon
"""Descriptive family outcomes for historical SKU episodes; not a causal or historical-glue reconstruction."""
import collections
import csv
import datetime as dt
import decimal
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price

OUT=pathlib.Path(__file__).parent;CAT=OUT.parent/'ozon_full_inventory_20261105';TZ=ZoneInfo('Europe/Moscow');D=decimal.Decimal
alias_model={};alias_primary={}
for f in (CAT/'raw').glob('info_*.json'):
    for x in json.loads(f.read_text())['response']['items']:
        model=str((x.get('model_info') or {}).get('model_id') or '');primary=str(x.get('sku') or '')
        for s in [x.get('sku')]+[v.get('sku') for v in x.get('sources',[])]:
            if s:alias_model[str(s)]=model;alias_primary[str(s)]=primary
orders=collections.defaultdict(set);values=collections.defaultdict(int);seen=set()
for w in json.loads((OUT/'order_collection_coverage.json').read_text())['windows']:
    z=json.loads(pathlib.Path(w['path']).read_text());assert z['complete']
    for r in z['postings']:
        key=(w['scheme'],r['posting_number']);assert key not in seen;seen.add(key)
        if r['status'].lower().startswith('cancel'):continue
        date=dt.datetime.fromisoformat((r.get('in_process_at') or r.get('created_at')).replace('Z','+00:00')).astimezone(TZ).date()
        for x in r['products']:
            model=alias_model.get(str(x['sku']))
            if not model:continue
            orders[(model,date)].add(str(r['order_id']));values[(model,date)]+=int((product_price(x)*D(str(x['quantity']))*100).to_integral_value())
def read(name):
    with (OUT/name).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
rare={r['sku'] for r in read('rare_both_modes_currently_in_stock.csv')};episodes=read('transition_episodes_currently_in_stock.csv');result=[]
for e in episodes:
    model=e['model_id_now'];start=dt.date.fromisoformat(e['first_positive_cpc_day']);n=int(e['window_days'])
    pre=[start-dt.timedelta(days=i) for i in range(n,0,-1)];post=[start+dt.timedelta(days=i) for i in range(n)]
    nb=len(set().union(*(orders.get((model,d),set()) for d in pre)));na=len(set().union(*(orders.get((model,d),set()) for d in post)))
    before=sum(values.get((model,d),0) for d in pre)/100;after=sum(values.get((model,d),0) for d in post)/100;bg=float(e['account_order_rate_factor'])
    result.append({'sku':e['sku'],'offer_id':e['offer_id'],'name':e['name'],'currently_in_stock':True,
                   'rare_both_modes_candidate':e['sku'] in rare,'model_id_current_snapshot':model,
                   'first_positive_cpc_day':e['first_positive_cpc_day'],'window_days':n,
                   'own_orders_before':e['orders_before'],'own_orders_after':e['orders_after'],
                   'family_orders_before':nb,'family_orders_after':na,'family_ordered_value_before_rub':before,'family_ordered_value_after_rub':after,
                   'neighbor_ordered_value_before_rub':round(before-float(e['ordered_value_before_rub']),2),
                   'neighbor_ordered_value_after_rub':round(after-float(e['ordered_value_after_rub']),2),
                   'family_order_rate_relative_to_account':na/nb/bg if nb else None,
                   'historical_family_membership_proven':False,'causal_own_or_family_ad_effect_proven':False})
with (OUT/'family_context_for_stock_episodes.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(result[0]));w.writeheader();w.writerows(result)
with (OUT.parent.parent/'experiments/cohorts/E8_treatment_2026-08-20.csv').open() as f:
    restored={alias_primary.get(r['sku'],r['sku']) for r in csv.DictReader(f) if r.get('account')=='oz_acc1' and r.get('applied','').lower()=='true'}
logged=[e for e in episodes if e['sku'] in restored and '2026-08-19'<=e['first_positive_cpc_day']<='2026-08-25']
with (OUT/'historical_logged_restore_matches.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(episodes[0]));w.writeheader();w.writerows(logged)
summary=json.loads((OUT/'analysis_summary.json').read_text());summary.update({'historical_E8_applied_restore_matched_stock_episodes':len(logged),
    'historical_E8_applied_restore_matched_stock_skus':len({e['sku'] for e in logged}),
    'rare_both_modes_skus_with_neighbor_sales_in_after_windows':len({r['sku'] for r in result if r['rare_both_modes_candidate'] and r['neighbor_ordered_value_after_rub']>0}),
    'family_context_is_current_mapping_not_historical_reconstruction':True});(OUT/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print('Family context rows',len(result),'historical applied-E8 matches',len(logged),'rare SKU with neighbor sales after',summary['rare_both_modes_skus_with_neighbor_sales_in_after_windows'])
