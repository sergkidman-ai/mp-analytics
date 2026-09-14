import csv,sys,collections
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q5_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:400]); out.write(s+'\n')
P('ozon_ads types', [(r['adv_type'],r['pay_model'],r['n'],r['sp']) for r in db.query("select adv_type,pay_model,count(distinct campaign_id) n, round(sum(spend)) sp from ozon_ads where account='oz_acc1' and period>='2026-08-20' group by 1,2")])
P('ozon_ads campaigns not in ads_sku_daily', [(r['campaign_id'],r['adv_type'],r['sp']) for r in db.query("""select campaign_id::text campaign_id, max(adv_type) adv_type, round(sum(spend)) sp from ozon_ads where account='oz_acc1' and period>='2026-08-20'
  and campaign_id::text not in (select distinct campaign_id::text from mkt_ozon_ads_sku_daily where account='oz_acc1' and stat_date>='2026-08-20') group by 1 having sum(spend)>0""")])
sp=db.query("select captured_at::date d, sku::text s, promo_status, bid, views_week from ozon_search_promo where account='oz_acc1'")
dates=sorted(set(r['d'] for r in sp)); P('search_promo snapshot dates',len(dates),[str(x) for x in dates][:12],'...',[str(x) for x in dates][-4:])
c=collections.Counter()
for r in sp:
    g=grp.get(r['s'])
    if g: c[(str(r['d']),g,r['promo_status'])]+=1
w=csv.writer(open(S+'search_promo_by_group.csv','w')); w.writerow(['date','group','status','n'])
for k,v in sorted(c.items()): w.writerow(list(k)+[v])
last=[k for k in c if k[0]==str(dates[-1])] if dates else []
P('search_promo last date by group/status',sorted((k,c[k]) for k in last))
first=[k for k in c if dates and k[0]==str(dates[0])]
P('search_promo first date',sorted((k,c[k]) for k in first))
