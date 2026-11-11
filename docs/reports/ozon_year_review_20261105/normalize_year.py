# поток: mkt / ozon
"""Annual CPC-exposure transitions and general sales. Observational only; never claims causal no-effect."""
import collections
import csv
import datetime as dt
import decimal
import json
import math
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price

OUT=pathlib.Path(__file__).parent
CAT=OUT.parent/'ozon_full_inventory_20261105'
START,END=dt.date(2025,11,5),dt.date(2026,11,4)
TZ=ZoneInfo('Europe/Moscow');D=decimal.Decimal
ALL_DAYS=[START+dt.timedelta(days=i) for i in range((END-START).days+1)]
date_cache={}
def day(value):
    if value not in date_cache:
        try:date_cache[value]=dt.datetime.strptime(value,'%d.%m.%Y').date()
        except ValueError:
            try:date_cache[value]=dt.date.fromisoformat(value[:10])
            except ValueError:date_cache[value]=None
    return date_cache[value]
def number(value):
    try:return float(str(value or '0').replace(' ','').replace('\xa0','').replace(',','.'))
    except ValueError:return 0.0
def cents(value):return int(round(number(value)*100))
def save(name,value):(OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def write(name,rows,fields):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

# Exact conditional binomial calculation for equal-window order-rate contrasts.
def logpmf(k,n,p):
    if p<=0:return 0.0 if k==0 else -math.inf
    if p>=1:return 0.0 if k==n else -math.inf
    return math.lgamma(n+1)-math.lgamma(k+1)-math.lgamma(n-k+1)+k*math.log(p)+(n-k)*math.log1p(-p)
def binom_sum(n,k,p):
    if k<0:return 0.0
    if k>=n:return 1.0
    values=[logpmf(i,n,p) for i in range(k+1)];m=max(values)
    return 0.0 if not math.isfinite(m) else min(1.0,math.exp(m)*sum(math.exp(x-m) for x in values))
def exact_p(k,n,p):
    if not n:return 1.0
    threshold=logpmf(k,n,p)+1e-10
    values=[logpmf(i,n,p) for i in range(n+1) if logpmf(i,n,p)<=threshold]
    if not values:return 1.0
    m=max(values);return min(1.0,math.exp(m)*sum(math.exp(x-m) for x in values))
def interval(k,n,alpha=.05):
    if not n:return 0.0,1.0
    if k==0:lower=0.0
    else:
        lo,hi=0.0,1.0
        for _ in range(42):
            mid=(lo+hi)/2
            if 1-binom_sum(n,k-1,mid)<alpha/2:lo=mid
            else:hi=mid
        lower=(lo+hi)/2
    if k==n:upper=1.0
    else:
        lo,hi=0.0,1.0
        for _ in range(42):
            mid=(lo+hi)/2
            if binom_sum(n,k,mid)>alpha/2:lo=mid
            else:hi=mid
        upper=(lo+hi)/2
    return lower,upper
assert abs(exact_p(0,10,.5)-.001953125)<1e-10
assert abs(interval(0,10)[1]-(1-.025**.1))<1e-8

status=json.loads((OUT/'sku_collection_status.json').read_text());assert status['complete']==status['jobs_expected']==16
catalog={};alias={}
for f in sorted((CAT/'raw').glob('info_*.json')):
    for x in json.loads(f.read_text())['response']['items']:
        sku=str(x.get('sku') or '')
        if not sku:continue
        stock=x.get('stocks') or {};qty=sum(max(0,number(s.get('present'))-number(s.get('reserved'))) for s in stock.get('stocks',[]))
        catalog[sku]={'sku':sku,'offer_id':x.get('offer_id',''),'name':x.get('name',''),'model_id':str((x.get('model_info') or {}).get('model_id') or ''),
                      'created_day':day(x.get('created_at','')),'currently_in_stock':bool(stock.get('has_stock') and qty>0 and not x.get('is_archived') and not x.get('is_autoarchived'))}
        for a in [x.get('sku')]+[s.get('sku') for s in x.get('sources',[])]:
            if a:alias[str(a)]=sku

ad=collections.defaultdict(lambda:[0,0,0,0]);ad_campaign_day=collections.defaultdict(lambda:[0,0,0])
traffic=collections.defaultdict(set);added=collections.defaultdict(set);ad_stats=collections.Counter();seen_ad=set()
def add_ad(cid,rawsku,date,views,clicks,spend,sales,created,source):
    if date is None or not START<=date<=END:return
    rawsku=str(rawsku)
    if not rawsku.isdigit():return
    ad_stats['reported_rows']+=1
    if not (views>0 or clicks>0 or spend>0):return
    ad_stats['positive_rows']+=1
    key=(cid,rawsku,date)
    assert key not in seen_ad, 'Duplicate raw SKU/campaign/day across report sources'
    seen_ad.add(key)
    sku=alias.get(rawsku,rawsku);k=(sku,date);v=ad[k]
    v[0]+=int(views);v[1]+=int(clicks);v[2]+=spend;v[3]+=sales;traffic[sku].add(date)
    t=ad_campaign_day[(cid,date)];t[0]+=int(views);t[1]+=int(clicks);t[2]+=spend
    created_day=day(created) if created else None
    if created_day and created_day.year>1900:
        added[(sku,cid)].add(created_day)
        if created_day>date:ad_stats['positive_rows_added_after_ad_day']+=1
for folder in sorted((OUT/'sku_reports').iterdir()):
    if not (folder/'complete.json').exists():continue
    for f in folder.glob('*.csv'):
        cid=f.name.split('_')[0]
        if not cid.isdigit():
            req=json.loads((folder/'complete.json').read_text())['request']['campaigns'];assert len(req)==1;cid=req[0]
        with f.open(encoding='utf-8-sig') as fh:
            next(fh,None)
            for r in csv.DictReader(fh,delimiter=';'):
                add_ad(cid,r.get('sku'),day(r.get('День','')),number(r.get('Показы')),number(r.get('Клики')),
                       cents(r.get('Расход, ₽, с НДС')),cents(r.get('Продажи в продвижении, ₽')),r.get('Дата добавления',''),str(f))
    print('Parsed annual SKU reports',folder.name,'positive rows so far',ad_stats['positive_rows'],flush=True)
for filename in json.loads((OUT/'sku_report_jobs.json').read_text())['reused_sku_report_files']:
    z=json.loads(pathlib.Path(filename).read_text())['body']
    for cid,block in z.items():
        for r in (block.get('report') or {}).get('rows',[]):
            add_ad(str(cid),r.get('sku'),day(r.get('date','')),number(r.get('views')),number(r.get('clicks')),cents(r.get('moneySpent')),cents(r.get('ordersMoney')),r.get('createdAt',''),filename)

# Only fully fetched, nonoverlapping posting windows count as covered days.
coverage=json.loads((OUT/'order_collection_coverage.json').read_text());assert coverage['finished']
covered={s:set() for s in ['fbs','fbo']};seen_postings=set();orders=collections.defaultdict(set)
values=collections.defaultdict(int);units=collections.defaultdict(float);account_orders=collections.defaultdict(set);account_values=collections.defaultdict(int)
post_stats=collections.Counter()
for w in coverage['windows']:
    z=json.loads(pathlib.Path(w['path']).read_text());assert z.get('complete')
    a,b=dt.date.fromisoformat(w['from_msk']),dt.date.fromisoformat(w['through_msk'])
    covered[w['scheme']].update(a+dt.timedelta(days=i) for i in range((b-a).days+1))
    for r in z['postings']:
        key=(w['scheme'],r['posting_number']);assert key not in seen_postings;seen_postings.add(key);post_stats['postings']+=1
        if r['status'].lower().startswith('cancel'):post_stats['cancelled_postings']+=1;continue
        date=dt.datetime.fromisoformat((r.get('in_process_at') or r.get('created_at')).replace('Z','+00:00')).astimezone(TZ).date()
        if not START<=date<=END:continue
        oid=str(r['order_id']);account_orders[date].add(oid)
        for item in r['products']:
            sku=alias.get(str(item['sku']),str(item['sku']));k=(sku,date);orders[k].add(oid)
            amount=int((product_price(item)*D(str(item['quantity']))*100).to_integral_value())
            values[k]+=amount;units[k]+=float(item['quantity']);account_values[date]+=amount
assert all(len(x)==365 for x in covered.values())

account_days={};campaign_rows=[]
with (OUT/'campaign_daily_year.csv').open() as f:
    for r in csv.DictReader(f):campaign_rows.append(r)
bad_ad_days=set();ad_reconciliation=[]
for r in campaign_rows:
    if r['type']!='SKU' or r['payment_type']!='CPC':continue
    date=day(r['date_msk']);reported=ad_campaign_day.get((r['campaign_id'],date),[0,0,0]);spend=cents(r['spend_rub']);views=int(number(r['views']))
    bad=(views>0 and reported[0]==0) or (spend>100 and reported[2]==0)
    if bad:bad_ad_days.add(date)
    if bad or abs(spend-reported[2])>100:
        ad_reconciliation.append({'campaign_id':r['campaign_id'],'date':str(date),'campaign_spend_rub':spend/100,'sku_spend_rub':reported[2]/100,'campaign_views':views,'sku_views':reported[0],'no_positive_sku_evidence':bad})

del seen_ad
ad_rows=({'sku':s,'date_msk':str(d),'views':v[0],'clicks':v[1],'spend_rub':v[2]/100,'attributed_sales_rub':v[3]/100} for (s,d),v in sorted(ad.items()))
write('sku_positive_cpc_daily_year.csv',ad_rows,['sku','date_msk','views','clicks','spend_rub','attributed_sales_rub'])
del ad_rows, ad, traffic, added
sales_rows=[{'sku':s,'date_msk':str(d),'unique_orders':len(v),'units':units[(s,d)],'ordered_value_rub':values[(s,d)]/100} for (s,d),v in sorted(orders.items())]
write('sku_general_sales_daily_year.csv',sales_rows,['sku','date_msk','unique_orders','units','ordered_value_rub'])
account_rows=[{'date_msk':str(d),'unique_orders':len(account_orders[d]),'ordered_value_rub':account_values[d]/100} for d in ALL_DAYS]
write('account_general_sales_daily_year.csv',account_rows,['date_msk','unique_orders','ordered_value_rub'])

# Replace the process so raw normalization memory is released before comparisons.
os.execv(sys.executable,[sys.executable,str(OUT/'analyze_year_streaming.py')])
