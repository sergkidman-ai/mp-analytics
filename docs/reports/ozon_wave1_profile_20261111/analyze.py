# поток: mkt / ozon
"""Offline analysis using the established matched-weekday/hour method and raw postings.
No API calls or financial DB writes. Ordered value is not realization revenue.
"""
import csv
import collections
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
from core.ozon_posting import product_price

P = Path(__file__).parent
TZ = ZoneInfo('Europe/Moscow')
sources = {
    'oz_acc1': (Path('docs/reports/ozon_launch_20261106/baseline'), Path('docs/reports/ozon_launch_20261106/uploaded_sku_manifest.csv')),
    'oz_acc2': (Path('docs/reports/ozon_acc2_monthly_experiment_20261108/activation_20261109/baseline'), Path('docs/reports/ozon_acc2_monthly_experiment_20261108/activation_20261109/launched_wave1.csv')),
}
load = lambda p: json.loads(p.read_text())


def posting_lines(files, maps):
    lines, seen, canceled = [], set(), 0
    for f in files:
        raw = load(f); assert raw['complete']
        scheme = raw.get('scheme') or f.stem.rsplit('_',1)[-1]
        for post in raw['postings']:
            key = (scheme, post['posting_number']); assert key not in seen; seen.add(key)
            if post['status'].lower().startswith('cancel'):
                canceled += 1; continue
            when = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z','+00:00'))
            for product in post['products']:
                sku = str(product['sku'])
                lines.append({'at':when, 'order':str(post['order_id']),
                              'sku':maps['canonical_by_alias'].get(sku,sku),
                              'model':maps['model_by_sku_alias'].get(sku),
                              'article':maps['article_family_by_sku_alias'].get(sku),
                              'value':product_price(product)*D(str(product['quantity']))})
    return lines, {'source_postings':len(seen),'canceled_postings_excluded':canceled,
                   'unmapped_model_product_lines':sum(not r['model'] for r in lines)}


out = {'causal_effect_identified':False, 'matching':'Four prior windows shifted by 7/14/21/28 days, same weekdays and hours',
       'current_postings_refreshed_including_cancellations':True, 'baseline_is_saved_prelaunch_snapshot':True,
       'ordered_value_is_not_financial_revenue':True, 'marketplace_writes':0, 'accounts':{}}
campaign_csv, profile_csv = [], []
for account, (base_dir, manifest_path) in sources.items():
    q=P/account; snap=load(q/'snapshot.json'); maps=load(Path(snap['family_maps_path']))
    start=dt.datetime.fromisoformat(snap['activation_at_utc']);end=dt.datetime.fromisoformat(snap['requested_at_utc'])
    assert end-dt.timedelta(days=7)<start
    ids={c['binding']['campaign_id'] for c in snap['campaigns']}
    with manifest_path.open(encoding='utf-8-sig') as f:
        cohort=[r for r in csv.DictReader(f) if r['actual_campaign_id'] in ids]
    target={maps['canonical_by_alias'].get(r['sku'],r['sku']) for r in cohort}
    models={maps['model_by_sku_alias'].get(r['sku']) for r in cohort}-{None,''}
    children={c['binding']['campaign_id'] for c in snap['campaigns'] if 'Дочерние' in c['binding']['title']}
    families={maps['article_family_by_sku_alias'].get(r['sku']) for r in cohort if r['actual_campaign_id'] in children}-{None,''}
    assert len(target)==(3451 if account=='oz_acc1' else 3851)
    base_files=[base_dir/'postings_fbs.json',base_dir/'postings_fbo.json']
    actual_files=[q/'postings_fbs.json',q/'postings_fbo.json']
    for f in base_files:
        coverage=load(f);lo=dt.datetime.combine(dt.date.fromisoformat(coverage['from_msk'][:10]),dt.time.min,TZ)
        hi=dt.datetime.combine(dt.date.fromisoformat(coverage['through_msk'][:10])+dt.timedelta(days=1),dt.time.min,TZ)
        assert start-dt.timedelta(days=28)>=lo and end-dt.timedelta(days=7)<hi
    base,base_quality=posting_lines(base_files,maps);actual,actual_quality=posting_lines(actual_files,maps)
    scopes={'account':lambda r:True,'wave_SKU':lambda r:r['sku'] in target,
            'native_models':lambda r:r['model'] in models,'child_article_families':lambda r:r['article'] in families}

    def metric(rows,lo,hi,scope):
        selected=[r for r in rows if lo<=r['at']<=hi and scopes[scope](r)]
        return {'orders':len({r['order'] for r in selected}), 'ordered_value_RUB':str(sum((r['value'] for r in selected),D(0)))}

    def compare(lo,hi,scope):
        current=metric(actual,lo,hi,scope)
        refs=[{'from_utc':(lo-dt.timedelta(days=7*i)).isoformat(),'through_utc':(hi-dt.timedelta(days=7*i)).isoformat(),
               **metric(base,lo-dt.timedelta(days=7*i),hi-dt.timedelta(days=7*i),scope)} for i in range(1,5)]
        means={k:sum(D(str(r[k])) for r in refs)/4 for k in current}
        return {'actual':current,'prior_windows':refs,'baseline4_mean':{k:str(v) for k,v in means.items()},
                'change_percent':{k:str((D(str(current[k]))/means[k]-1)*100) if means[k] else None for k in current}}

    comparison={scope:compare(start,end,scope) for scope in scopes}
    def concentration(rows,lo,hi):
        baskets=collections.defaultdict(lambda:D(0))
        for r in rows:
            if lo<=r['at']<=hi:
                baskets[r['order']]+=r['value']
        values=sorted(baskets.values(),reverse=True);total=sum(values,D(0))
        return {'orders':len(values),'ordered_value_RUB':str(total),
                'largest_order_RUB':str(values[0]) if values else None,
                'top3_ordered_value_RUB':str(sum(values[:3],D(0))),
                'top3_share_pct':str(sum(values[:3],D(0))/total*100) if total else None,
                'value_excluding_largest_RUB':str(total-values[0]) if values else '0'}
    actual_mix=concentration(actual,start,end)
    base_mix=[concentration(base,start-dt.timedelta(days=7*i),end-dt.timedelta(days=7*i)) for i in range(1,5)]
    base_without_largest=sum(D(r['value_excluding_largest_RUB']) for r in base_mix)/4
    mix={'actual':actual_mix,'prior_windows':base_mix,
         'change_excluding_each_windows_largest_pct':str((D(actual_mix['value_excluding_largest_RUB'])/base_without_largest-1)*100) if base_without_largest else None}
    daily=[];day=start.astimezone(TZ).date()
    while day<=end.astimezone(TZ).date():
        lo=max(start,dt.datetime.combine(day,dt.time.min,TZ))
        hi=min(end,dt.datetime.combine(day+dt.timedelta(days=1),dt.time.min,TZ)-dt.timedelta(microseconds=1))
        daily.append({'date_msk':str(day),'from_msk':lo.astimezone(TZ).isoformat(),'through_msk':hi.astimezone(TZ).isoformat(),
                      'partial':lo.astimezone(TZ).time()!=dt.time.min or hi.astimezone(TZ).time()!=dt.time(23,59,59,999999),
                      **compare(lo,hi,'account')})
        day+=dt.timedelta(days=1)
    campaign_rows=[]
    for c in snap['campaigns']:
        b=c['binding'];rows=[r for r in snap['daily']['rows'] if str(r['id'])==b['campaign_id']];assert rows
        assert c['live']['state']=='CAMPAIGN_STATE_RUNNING'
        total=lambda k:sum((D(str(r[k]).replace(' ','').replace(',','.')) for r in rows),D(0))
        value=total('ordersMoney');spend=total('moneySpent')
        rec={'account':account,'campaign_id':b['campaign_id'],'group':b['title'].split('|')[2].strip(),
             'cohort_SKU':b['uploaded_skus'],'state':c['live']['state'],'spend_RUB':str(spend),
             'views':int(total('views')),'clicks':int(total('clicks')),'ad_orders':int(total('orders')),
             'ad_order_value_RUB':str(value),'ad_DRR_percent':str(spend/value*100) if value else None}
        campaign_rows.append(rec);campaign_csv.append(rec)
    totals={k:str(sum(D(str(r[k])) for r in campaign_rows)) for k in ['cohort_SKU','spend_RUB','views','clicks','ad_orders','ad_order_value_RUB']}
    acc={'from_utc':start.isoformat(),'through_utc':end.isoformat(),'source_baseline':[str(f) for f in base_files],
         'source_actual':[str(f) for f in actual_files],'baseline_quality':base_quality,'actual_quality':actual_quality,
         'cohort_SKUs':len(target),'native_models':len(models),'child_article_families':len(families),
         'campaigns':campaign_rows,'ad_totals':totals,'profile_scopes':comparison,'account_days':daily,'order_concentration':mix,
         'guard_checked_at_utc':snap['guard_checked_at_utc'],'guard_status':snap['guard_status'],
         'guard_actions':snap['guard_actions'],'calendar_stop_at_msk':snap['calendar_stop_at_msk']}
    out['accounts'][account]=acc
    for scope,stats in comparison.items():
        profile_csv.append({'account':account,'scope':scope,'from_utc':start.isoformat(),'through_utc':end.isoformat(),
                            'actual_orders':stats['actual']['orders'],'baseline4_mean_orders':stats['baseline4_mean']['orders'],
                            'orders_change_percent':stats['change_percent']['orders'],'actual_ordered_value_RUB':stats['actual']['ordered_value_RUB'],
                            'baseline4_mean_ordered_value_RUB':stats['baseline4_mean']['ordered_value_RUB'],
                            'value_change_percent':stats['change_percent']['ordered_value_RUB']})
    print(account,'ad totals',totals,'profile',comparison['account']['actual'],'baseline',comparison['account']['baseline4_mean'],
          'change%',comparison['account']['change_percent'],'actual quality',actual_quality,flush=True)
out['known_confounders']=['No randomized/control assignment','30 October bid reductions for20SKU on acc1',
                         '8 November additional acc1 bid changes','Holiday4November in some reference windows',
                         '11November promotions/seasonality','Other active CPC and CPO campaigns',
                         'Acc2 autoarchive recoveries changed availability','Historical availability/model maps not reconstructed',
                         'Baseline cancellations are as saved before launch, not newly refreshed']
(P/'analysis.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
for filename,rows in [('campaigns.csv',campaign_csv),('profile_comparison.csv',profile_csv)]:
    with (P/filename).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
