# поток: mkt / ozon
"""Offline fixed20 report: equal full-day periods, all observed placements, no API."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D
from html import escape
import json
from pathlib import Path

O=Path(__file__).parent;ROOT=O.parent
read=lambda f:list(csv.DictReader(f.open(encoding='utf-8-sig')))
quality=json.loads((O/'monitor20_quality.json').read_text())
assert quality['posting_snapshots_complete'] and quality['ad_sku_day_coverage']==220
main=read(ROOT/'revised_sku_summary_20.csv');target={r['sku'] for r in main};models={r['current_family_model_id'] for r in main}
assert len(target)==20 and len(models)==19
before_lines=read(ROOT.parent/'ozon_launch_20261106/baseline/sales_lines.csv')
for r in before_lines:r['current_model_id']=r['model_id']
after_lines=read(O/'refreshed_sales_lines.csv');ads=read(O/'merged_ads.csv')
for r in before_lines+after_lines:r['ordered_value_rub']=D(r['ordered_value_rub'])
for r in ads:
    for k in ['spend_rub','ad_revenue_rub','ad_orders_api','views','clicks']:r[k]=D(r[k])
dates=lambda lo,hi:[(dt.date.fromisoformat(lo)+dt.timedelta(days=i)).isoformat() for i in range((dt.date.fromisoformat(hi)-dt.date.fromisoformat(lo)).days+1)]
spec=[('before','2026-10-19','2026-10-29',before_lines),('after','2026-10-31','2026-11-10',after_lines)]


def sales(rows,days,scope):
    selected=[r for r in rows if r['date_msk'] in days and (scope=='account' or scope=='own' and r['sku'] in target or scope=='models' and r['current_model_id'] in models)]
    return {'orders':len({r['order_id'] for r in selected}),'ordered_value_RUB':str(sum((r['ordered_value_rub'] for r in selected),D(0)))}


def period(phase,lo,hi,lines):
    ds=dates(lo,hi);aa=[r for r in ads if r['date_msk'] in ds and r['sku'] in target]
    sums={k:sum((r[k] for r in aa),D(0)) for k in ['spend_rub','ad_revenue_rub','ad_orders_api','views','clicks']}
    drr=sums['spend_rub']/sums['ad_revenue_rub']*100 if sums['ad_revenue_rub'] else None
    cpc=sums['spend_rub']/sums['clicks'] if sums['clicks'] else None
    observed={(r['sku'],r['date_msk']) for r in aa}
    return {'phase':phase,'from_msk':lo,'through_msk':hi,'days':len(ds),
            'ad':{k:str(v) for k,v in sums.items()},'ad_DRR_percent':str(drr) if drr is not None else None,
            'average_CPC_RUB':str(cpc) if cpc is not None else None,
            'observed_placements':len({(r['sku'],r['campaign_id']) for r in aa}),
            'observed_sku_days':len(observed),'expected_sku_days':len(ds)*20,
            'missing_sku_days':[{'sku':s,'date_msk':d} for s in sorted(target) for d in ds if (s,d) not in observed],
            'sales':{scope:sales(lines,ds,scope) for scope in ['own','models','account']}}


periods=[period(*s) for s in spec];b,a=periods
change=lambda actual,baseline:str((D(str(actual))/D(str(baseline))-1)*100) if D(str(baseline)) else None
changes={'ad_spend_percent':change(a['ad']['spend_rub'],b['ad']['spend_rub']),
         'observed_ad_spend_difference_RUB':str(D(b['ad']['spend_rub'])-D(a['ad']['spend_rub'])),
         'average_CPC_percent':change(a['average_CPC_RUB'],b['average_CPC_RUB'])}
for scope in ['own','models','account']:
    for metric in ['orders','ordered_value_RUB']:
        changes[scope+'_'+metric+'_percent']=change(a['sales'][scope][metric],b['sales'][scope][metric])
per_sku=[]
for item in main:
    s=item['sku'];row={'sku':s,'article':item['article'],'model_id':item['current_family_model_id']}
    for phase,lo,hi,lines in spec:
        ds=dates(lo,hi);ar=[r for r in ads if r['sku']==s and r['date_msk'] in ds];ll=[r for r in lines if r['sku']==s and r['date_msk'] in ds]
        spend=sum((r['spend_rub'] for r in ar),D(0));val=sum((r['ad_revenue_rub'] for r in ar),D(0))
        row.update({phase+'_ad_spend_RUB':str(spend) if ar else None,phase+'_observed_ad_days':len({r['date_msk'] for r in ar}),
                    phase+'_ad_order_value_RUB':str(val) if ar else None,phase+'_ad_DRR_percent':str(spend/val*100) if val else None,
                    phase+'_own_orders':len({r['order_id'] for r in ll}),phase+'_own_ordered_value_RUB':str(sum((r['ordered_value_rub'] for r in ll),D(0)))})
    row['observed_spend_difference_RUB']=str(D(row['before_ad_spend_RUB'])-D(row['after_ad_spend_RUB']))
    row['own_value_change_RUB']=str(D(row['after_own_ordered_value_RUB'])-D(row['before_own_ordered_value_RUB']))
    per_sku.append(row)
assert sum(D(r['after_ad_spend_RUB']) for r in per_sku)==D(a['ad']['spend_rub'])
assert sum(D(r['before_ad_spend_RUB']) for r in per_sku)==D(b['ad']['spend_rub'])
assert sum(D(r['after_own_ordered_value_RUB']) for r in per_sku)==D(a['sales']['own']['ordered_value_RUB'])
latest7=[period('before','2026-10-23','2026-10-29',before_lines),period('after','2026-11-04','2026-11-10',after_lines)]
legacy=read(ROOT/'revised_sales_lines.csv')
for r in legacy:r['ordered_value_rub']=D(r['ordered_value_rub'])
base28=dates('2026-10-02','2026-10-29');latest_days=dates('2026-11-04','2026-11-10');latest_matched={}
for scope in ['own','models','account']:
    expected={k:sum((sum((D(str(sales(legacy,[b],scope)[k])) for b in base28 if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(day).weekday()),D(0))/4 for day in latest_days),D(0)) for k in ['orders','ordered_value_RUB']}
    current=sales(after_lines,latest_days,scope)
    latest_matched[scope]={'actual':current,'expected_same_weekdays':{k:str(v) for k,v in expected.items()},'change_percent':{k:change(current[k],expected[k]) for k in current}}
matched=[r for r in read(O/'monitor20_sales_comparison.csv') if r['baseline_days']=='28']
status={'account':'oz_acc1','fixed_SKUs':20,'frozen_model_groups':19,'known_campaign_SKU_bindings':36,
        'periods':periods,'changes':changes,'latest_equal7_complete_weekdays':latest7,'latest7_weekday_matched28':latest_matched,'weekday_matched28_general_sales':matched,
        'observed_pairs_in_all_saved_history':len({(r['sku'],r['campaign_id']) for r in ads}),
        'quality':quality,'all36_history_complete':False,'missing_is_zero':False,'cut_day_20261030_excluded':True,
        'ordered_value_is_financial_revenue':False,'causal_effect_identified':False,'marketplace_mutations':0,
        'baseline_general_source':'ozon_launch_20261106/baseline/sales_lines.csv',
        'baseline_ad_source':'revised_sku_campaign_daily.csv','actual_general_source':'refreshed_sales_lines.csv',
        'data_refreshed_at_utc':json.loads((O/'raw/monitor_postings_20261031_20261110_fbs.json').read_text())['retrieved_at_utc'],
        'confounders':['Weekday mix differs between the equal11day periods','Holiday4November',
                       'Wave1 from6November overlaps5of19modelgroups','Two further acc1bidchanges8November',
                       'Baseline cancellations as saved before wave launch','Late ad attribution can change']}
(O/'status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
for fn,rows in [('by_sku_20.csv',per_sku),('equal11_periods.csv',[{**{k:v for k,v in r.items() if k not in ['ad','sales','missing_sku_days']},**r['ad'],**{scope+'_'+k:v for scope,rr in r['sales'].items() for k,v in rr.items()}} for r in periods])]:
    with (O/fn).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
print(json.dumps({'periods':periods,'changes':changes},ensure_ascii=False))
