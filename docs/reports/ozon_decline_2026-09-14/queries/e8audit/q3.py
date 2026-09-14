import csv,sys,collections
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
F='/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv'
rows=list(csv.DictReader(open(F,encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}; links={(r['campaign_id'],r['sku']):r['group'] for r in rows}
out=open(S+'q3_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:300]); out.write(s+'\n')
# reproduce cohort: before 08.08 minus after 09.08
b=db.query("select distinct campaign_id::text c, sku::text s from ozon_bids where account='oz_acc1' and captured_at::date='2026-08-08'")
a=set((r['c'],r['s']) for r in db.query("select distinct campaign_id::text c, sku::text s from ozon_bids where account='oz_acc1' and captured_at::date='2026-08-09'"))
coh=set((r['c'],r['s']) for r in b)-a
P('reproduced before-after',len(coh),'csv in it',sum(1 for k in links if k in coh),'coh not in csv',len(coh-set(links)))
# daily presence by group (sku-level, any campaign) + in own link
bids=db.query("select captured_at::date d, campaign_id::text c, sku::text s, state, adv_type from ozon_bids where account='oz_acc1' and captured_at>='2026-08-07' ")
day=collections.defaultdict(lambda: collections.defaultdict(set)); advt=collections.Counter()
for r in bids:
    g=grp.get(r['s'])
    if not g: continue
    day[r['d']][g].add(r['s'])
    if (r['c'],r['s']) in links: day[r['d']][g+'_ownlink'].add((r['c'],r['s']))
    else: day[r['d']][g+'_otherlink'].add((r['c'],r['s']))
    if g=='B' and str(r['d'])>='2026-08-20': advt[(r['c'],r['adv_type'],r['state'])]+=1
w=csv.writer(open(S+'presence_by_day.csv','w'))
w.writerow(['date','A_sku','B_sku','A_ownlink','B_ownlink','A_otherlink','B_otherlink'])
for d in sorted(day):
    x=day[d]; w.writerow([d,len(x['A']),len(x['B']),len(x['A_ownlink']),len(x['B_ownlink']),len(x['A_otherlink']),len(x['B_otherlink'])])
for d in sorted(day):
    if str(d) in ('2026-08-07','2026-08-08','2026-08-09','2026-08-18','2026-08-19','2026-08-20','2026-08-21','2026-09-01','2026-09-14'):
        x=day[d]; P(d,'A',len(x['A']),'B',len(x['B']),'Aown',len(x['A_ownlink']),'Bown',len(x['B_ownlink']),'Aoth',len(x['A_otherlink']),'Both',len(x['B_otherlink']))
bmax=max((len(day[d]['B']),d) for d in day if str(d)>='2026-08-20'); P('B max sku in bids after 20.08',bmax)
P('B campaigns after 20.08 (top)',advt.most_common(8))
# ads daily
ad=db.query("select stat_date d, sku::text s, campaign_id::text c, views, clicks, money_spent m, orders_qty q, collected_at from mkt_ozon_ads_sku_daily where account='oz_acc1' and stat_date>='2026-07-27'")
agg=collections.defaultdict(lambda:[0,0,0.0,0,set()]); bcamp=collections.Counter(); late=collections.Counter()
for r in ad:
    g=grp.get(r['s'])
    if not g: continue
    k=(r['d'],g); agg[k][0]+=r['views'] or 0; agg[k][1]+=r['clicks'] or 0; agg[k][2]+=float(r['m'] or 0); agg[k][3]+=r['q'] or 0; agg[k][4].add(r['s'])
    if g=='B' and str(r['d'])>='2026-08-20' and ((r['views'] or 0)>0 or float(r['m'] or 0)>0): bcamp[r['c']]+=1
w=csv.writer(open(S+'ads_by_group_day.csv','w')); w.writerow(['date','group','views','clicks','spent','orders','n_sku'])
for k in sorted(agg): w.writerow([k[0],k[1]]+agg[k][:4]+[len(agg[k][4])])
bpost=[(k[0],agg[k][0],round(agg[k][2]),len(agg[k][4])) for k in sorted(agg) if k[1]=='B' and str(k[0])>='2026-08-20' and (agg[k][0]>0 or agg[k][2]>0)]
P('B days with views/spend after 20.08:',len(bpost),bpost[:6])
P('B campaigns with activity',bcamp.most_common(10))
ad_days=sorted(set(r['d'] for r in ad)); import datetime as dt
alld=[ad_days[0]+dt.timedelta(i) for i in range((ad_days[-1]-ad_days[0]).days+1)]
P('ads_daily days',ad_days[0],ad_days[-1],'missing',[str(x) for x in alld if x not in ad_days])
