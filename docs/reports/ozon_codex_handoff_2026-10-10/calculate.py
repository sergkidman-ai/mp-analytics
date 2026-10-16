# поток: mkt — descriptive calculations on frozen cohorts; no canonical writes
import sys, pathlib, json, csv, re, collections
import pandas as pd
sys.path.insert(0,str(pathlib.Path(__file__).parent))
from read_audit import query, save, OUT
EXP=pathlib.Path('/opt/mp-analytics/docs/experiments')
posts=pd.DataFrame(query("""SELECT account,posting_number,scheme,status,(in_process_at AT TIME ZONE 'Europe/Moscow')::date AS d,
 coalesce(nullif(payload->>'order_number',''),posting_number) buyer_order,
 p->>'sku' sku,p->>'offer_id' offer_id,(p->>'quantity')::numeric qty,(p->>'price')::numeric price
 FROM raw_ozon_posting CROSS JOIN LATERAL jsonb_array_elements(payload->'products') p
 WHERE in_process_at>='2026-07-26' AND in_process_at<'2026-10-10 21:00:00+00'"""))
ads=pd.DataFrame(query("SELECT account,stat_date AS d,campaign_id,sku::text sku,views,clicks,money_spent spend,orders_qty ad_orders,orders_money ad_revenue FROM mkt_ozon_ads_sku_daily WHERE stat_date>='2026-07-27'"))
products=pd.DataFrame(query('SELECT account,sku::text sku,offer_id FROM ozon_product'))
ret=pd.DataFrame(query("""SELECT r.account,r.return_id,r.return_type,r.order_number,r.status_raw,r.stage,
 (r.created_at AT TIME ZONE 'Europe/Moscow')::date AS d,i.sku::text sku,i.offer_id,i.qty,i.price
 FROM mp_returns r JOIN mp_return_items i USING(platform,account,return_id)
 WHERE platform='ozon' AND r.created_at>='2026-07-27'"""))
for frame in [posts,ads,ret]:
    frame.rename(columns={'d':'day'},inplace=True)
    frame['day']=pd.to_datetime(frame.day)
    for col in ['qty','price','views','clicks','spend','ad_orders','ad_revenue']:
        if col in frame:frame[col]=pd.to_numeric(frame[col],errors='coerce')
for frame in [posts,products,ret]:
    frame['family']=frame.offer_id.fillna('').str[:4]
    frame['offer_base']=frame.offer_id.fillna('').str.replace(r'X\d+$','',regex=True)
    frame['bundle_n']=frame.offer_id.fillna('').str.extract(r'X(\d+)$')[0].fillna('1').astype(int)
posts['gross']=posts.qty*posts.price
ret['gross']=ret.qty*ret.price
ads=ads.merge(products.drop_duplicates(['account','sku'])[['account','sku','family','offer_base','bundle_n']],on=['account','sku'],how='left')
save('posting_lines',posts.to_dict('records'));save('return_lines',ret.to_dict('records'))

def window(df,a,b):return df[(df.day>=a)&(df.day<=b)]
def metrics(acc,skus,a,b, family=None, bundles=None):
    pp=posts[posts.account==acc];aa=ads[ads.account==acc];rr=ret[ret.account==acc]
    if skus is not None:
        pp=pp[pp.sku.isin(skus)];aa=aa[aa.sku.isin(skus)];rr=rr[rr.sku.isin(skus)]
    if family is not None:
        pp=pp[pp.family.isin(family)];aa=aa[aa.family.isin(family)];rr=rr[rr.family.isin(family)]
    if bundles is not None:
        pp=pp[pp.bundle_n.gt(1)==bundles];aa=aa[aa.bundle_n.gt(1)==bundles];rr=rr[rr.bundle_n.gt(1)==bundles]
    pp=window(pp,a,b);aa=window(aa,a,b);rr=window(rr,a,b)
    good=pp[pp.status!='cancelled'];delivered=pp[pp.status=='delivered'];bad=pp[pp.status=='cancelled']
    n=(pd.Timestamp(b)-pd.Timestamp(a)).days+1
    r=dict(account=acc,from_day=a,to_day=b,days=n,n_skus=len(skus) if skus is not None else None,
     noncancel_postings=good.posting_number.nunique(),noncancel_buyer_orders=good.buyer_order.nunique(),
     units=good.qty.sum(),gross=good.gross.sum(),delivered_postings=delivered.posting_number.nunique(),
     delivered_units=delivered.qty.sum(),delivered_gross=delivered.gross.sum(),
     cancelled_postings=bad.posting_number.nunique(),cancelled_units=bad.qty.sum(),cancelled_gross=bad.gross.sum(),
     return_events=rr.return_id.nunique(),return_units=rr.qty.sum(),return_gross=rr.gross.sum(),
     return_types=rr.groupby('return_type').qty.sum().to_dict(),
     views=aa.views.sum(),clicks=aa.clicks.sum(),spend=aa.spend.sum(),
     ad_orders=aa.ad_orders.sum(),ad_revenue=aa.ad_revenue.sum(),ad_sku_days=aa[['sku','day']].drop_duplicates().shape[0])
    r['CPC']=r['spend']/r['clicks'] if r['clicks'] else None
    r['postings_day']=r['noncancel_postings']/n;r['gross_day']=r['gross']/n
    return r

def load_cohort(name):return [r['sku'] for r in csv.DictReader((EXP/'cohorts'/name).open())]
result={}
result['experiments']={}
configs={'E5':{'T':'E5_treatment_2026-08-20.csv','C':'E5_control_2026-08-20.csv','pre':['2026-08-11','2026-08-17'],'post':['2026-08-18','2026-09-07']},
         'E7':{'T':'E7_treatment_2026-08-20.csv','pre':['2026-08-12','2026-08-18'],'post':['2026-08-19','2026-09-01']},
         'H-001':{'T':'HALO_CORE_A_STABLE_ADVERTISED_2026-08-21.csv','C':'HALO_BASELINE_NEVER_ADVERTISED_2026-08-21.csv','pre':['2026-07-27','2026-08-08'],'post':['2026-08-20','2026-09-13']}}
for eid,cfg in configs.items():
    g={}
    for arm in ['T','C']:
        if arm not in cfg:continue
        skus=load_cohort(cfg[arm]);g[arm]={}
        for label,w in [('pre',cfg['pre']),('contract_post',cfg['post']),('latest7',['2026-10-03','2026-10-09'])]:g[arm][label]=metrics('oz_acc1',skus,*w)
        if eid=='H-001':
            for label,w in [('B',['2026-08-10','2026-08-18']),('mature_check',['2026-08-21','2026-08-26'])]:g[arm][label]=metrics('oz_acc1',skus,*w)
        # Actual original campaigns/bids + every subsequent snapshot, separated from assignment.
        save(eid+'_'+arm+'_bids',query("SELECT campaign_id,campaign_title,sku::text sku,bid,captured_at,state FROM ozon_bids WHERE account='oz_acc1' AND sku::text=ANY(%s) ORDER BY captured_at,sku,campaign_id",(skus,)))
    result['experiments'][eid]=g
result['weekly_orders']=[]
for acc in ['oz_acc1','oz_acc2']:
    for a,b in [('2026-09-19','2026-09-25'),('2026-09-26','2026-10-02'),('2026-10-03','2026-10-09')]:result['weekly_orders'].append(metrics(acc,None,a,b))

api=json.loads((OUT/'api_audit.json').read_text())
bundle_skus=[str(x['sku']) for page in api['oz_acc1']['products']['35269713'] for x in page['body'].get('products',[])]
families=set(products[(products.account=='oz_acc1')&products.sku.isin(bundle_skus)].family)
save('bundle_families',sorted(families));result['bundle_campaign_skus']=len(bundle_skus);result['bundle_families']=len(families)
result['bundles']=[]
for a,b in [('2026-08-08','2026-10-09'),('2026-09-14','2026-10-09'),('2026-09-26','2026-10-02'),('2026-10-03','2026-10-09')]:
    for kind,bundle in [('all_family',None),('single',False),('bundle',True)]:
        r=metrics('oz_acc1',None,a,b,family=families,bundles=bundle);r['kind']=kind;result['bundles'].append(r)
result['bundle_ads_campaign']=[]
for a,b in [('2026-08-08','2026-10-09'),('2026-10-03','2026-10-09')]:
    x=window(ads[(ads.account=='oz_acc1')&(ads.campaign_id.astype(str)=='35269713')],a,b)
    result['bundle_ads_campaign'].append({'from':a,'to':b,'views':x.views.sum(),'clicks':x.clicks.sum(),'spend':x.spend.sum(),'ad_orders':x.ad_orders.sum(),'ad_revenue':x.ad_revenue.sum()})
result['all_bundle_sales']=[]
for acc in ['oz_acc1','oz_acc2']:
    for a,b in [('2026-08-08','2026-10-09'),('2026-09-26','2026-10-02'),('2026-10-03','2026-10-09')]:result['all_bundle_sales'].append(metrics(acc,None,a,b,bundles=True))
result['bundle_ad_campaigns']=query("SELECT a.account,a.campaign_id,min(a.stat_date) first_day,max(a.stat_date) last_day,count(distinct a.sku) bundle_skus,sum(a.money_spent) spend,sum(a.orders_qty) ad_orders FROM mkt_ozon_ads_sku_daily a JOIN ozon_product p ON a.account=p.account AND a.sku=p.sku WHERE p.offer_id~'X[0-9]+$' GROUP BY 1,2 ORDER BY 1,3")

assignment=list(csv.DictReader((EXP/'ozon_acc2_bid_dose_2026-09-21/assignment.csv').open()))
result['dose_status_metrics']={arm:metrics('oz_acc2',[r['sku'] for r in assignment if r['arm']==arm],'2026-09-21','2026-10-09') for arm in ['C','L','H']}
save('calculations',result)
for r in result['weekly_orders']:print(r['account'],r['from_day'],r['noncancel_postings'],round(r['gross']),r['cancelled_postings'])
print('bundles',len(bundle_skus),'SKU',len(families),'families')
for eid,g in result['experiments'].items():
    print(eid,[(a,x['contract_post']['noncancel_postings'],round(x['contract_post']['spend'])) for a,x in g.items()])
