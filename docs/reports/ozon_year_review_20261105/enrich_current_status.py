# поток: mkt / ozon
"""Join current stock/CPC and draft pool, keeping historical episodes as observations."""
import collections
import csv
import decimal
import json
import pathlib

OUT=pathlib.Path(__file__).parent;CAT=OUT.parent/'ozon_full_inventory_20261105';D=decimal.Decimal
def read(path):
    with path.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
def write(path,rows):
    if not rows:return
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
alias={}
for r in read(CAT/'all_current_catalog.csv'):
    for s in r['sku_aliases'].split(';'):alias[s]=r['sku']
active={alias.get(s,s) for s in json.loads((CAT/'current_active_cpc.json').read_text())['skus']}
pool={r['sku'] for r in read(CAT/'in_stock/all_outside_cpc.csv')}
for name in ['sku_transition_summary_in_stock.csv','transition_episodes_currently_in_stock.csv',
             'annual_sku_profile_currently_in_stock.csv','selling_without_observed_cpc_in_stock.csv','no_usable_off_on_comparison_in_stock.csv']:
    rows=read(OUT/name)
    for r in rows:r.update(currently_in_running_CPC=r['sku'] in active,in_new_monthly_pool=r['sku'] in pool)
    write(OUT/name,rows)
summary=json.loads((OUT/'analysis_summary.json').read_text())
sku_rows=read(OUT/'sku_transition_summary_in_stock.csv');episodes=read(OUT/'transition_episodes_currently_in_stock.csv')
by_sku=collections.defaultdict(list)
for e in episodes:by_sku[e['sku']].append(e)
rare=[r for r in sku_rows if r['preliminary_category']=='RARE_BOTH_MODES_EFFECT_UNPROVEN'];rare_set={r['sku'] for r in rare}
rare_with_sales_before={s for s in rare_set if any(int(e['orders_before'])>0 for e in by_sku[s])}
rare_zero_both={s for s in rare_set if all(int(e['orders_before'])==0 and int(e['orders_after'])==0 for e in by_sku[s])}
write(OUT/'rare_both_modes_currently_in_stock.csv',rare)
review=[]
for s,ee in by_sku.items():
    valid=[e for e in ee if all(e[k]=='False' for k in ['creation_date_after_historical_exposure','new_card_without_full_pre_window','ad_coverage_issue_in_window'])]
    q=[e for e in valid if int(e['orders_before'])>0 and float(e['cpc_spend_after_rub'])>0 and e['relative_order_rate_factor'] and float(e['relative_order_rate_factor'])<=1.2 and int(e['cpc_traffic_days_after'])>=min(7,int(e['window_days']))]
    if not q:continue
    base=q[0]
    review.append({'sku':s,'offer_id':base['offer_id'],'name':base['name'],'currently_in_stock':True,
                   'currently_in_running_CPC':s in active,'in_new_monthly_pool':s in pool,
                   'episodes_with_sales_before_CPC_and_no_observed_gain':len(q),'all_valid_episodes':len(valid),
                   'some_other_valid_episodes_show_observed_gain':any(e['relative_order_rate_factor'] and float(e['relative_order_rate_factor'])>1.2 for e in valid),
                   'frequent_demand_before_in_some_qualified_episode':any(e['demand_before']=='frequent_8plus_per_28d' for e in q),
                   'causal_zero_effect_proven':False,'review_only_no_automatic_disable':True})
write(OUT/'selling_before_cpc_no_observed_gain_review.csv',review)
profile=read(OUT/'annual_sku_profile_currently_in_stock.csv')
summary.update({'rare_both_modes_skus_with_some_sales_before_CPC':len(rare_with_sales_before),
                'rare_both_modes_skus_zero_sales_in_both_all_compared_windows':len(rare_zero_both),
                'rare_both_modes_skus_currently_in_CPC':sum(r['sku'] in active for r in rare),
                'rare_both_modes_skus_in_new_monthly_pool':sum(r['sku'] in pool for r in rare),
                'selling_before_CPC_no_observed_gain_review_skus':len(review),
                'frequent_sellers_in_no_observed_gain_review':sum(r['frequent_demand_before_in_some_qualified_episode'] for r in review),
                'annual_CPC_spend_current_stock_rub':str(sum((D(r['annual_cpc_spend_rub']) for r in profile),D(0))),
                'annual_CPC_spend_currently_active_rare_both_modes_rub':str(sum((D(r['annual_cpc_spend_rub']) for r in rare if r['sku'] in active),D(0)))})
(OUT/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
indexed={r['sku']:r for r in sku_rows};waves=read(OUT.parent/'ozon_monthly_experiment_20261105/wave_draft_all_candidates.csv');joined=[]
for w in waves:
    h=indexed.get(w['sku'],{})
    joined.append({'sku':w['sku'],'offer_id':w['offer_id'],'model_id':w['model_id'],'draft_wave':w['draft_wave'],
                   'segment':w['segment'],'segment_group':w.get('segment_group',''),
                   'is_child_article':w.get('is_child_article',''),
                   'parent_offer_id':w.get('parent_offer_id',''),
                   'parent_sku':w.get('parent_sku',''),
                   'parent_product_id':w.get('parent_product_id',''),
                   'article_family_key':w.get('article_family_key',''),
                   'historical_review_category':h.get('preliminary_category','NO_QUALIFYING_TRANSITION'),
                   'historical_transition_episodes':h.get('observed_transition_episodes',0),
                   'annual_cpc_spend_rub':h.get('annual_cpc_spend_rub',''),
                   'historical_causal_effect_proven':False,'draft_assignment_changed_by_history':False})
write(OUT.parent/'ozon_monthly_experiment_20261105/year_flags_for_wave_draft.csv',joined)
print('Year review enrichments:',{k:summary[k] for k in ['rare_both_modes_skus_with_some_sales_before_CPC','rare_both_modes_skus_zero_sales_in_both_all_compared_windows','rare_both_modes_skus_currently_in_CPC','rare_both_modes_skus_in_new_monthly_pool','selling_before_CPC_no_observed_gain_review_skus','frequent_sellers_in_no_observed_gain_review','annual_CPC_spend_current_stock_rub']})
