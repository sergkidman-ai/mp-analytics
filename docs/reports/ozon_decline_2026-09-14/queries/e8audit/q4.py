import csv,sys,collections
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q4_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:350]); out.write(s+'\n')
for t in ['ozon_search_promo','ozon_ads','ozon_search_product','oz_action_log','ozon_product']:
    P(t,[c['column_name'] for c in db.query("select column_name from information_schema.columns where table_name=%s order by ordinal_position",(t,))])
P('bids adv_type 08-20', [(r['adv_type'],r['state'],r['n'],r['nc']) for r in db.query("select adv_type,state,count(*) n,count(distinct campaign_id) nc from ozon_bids where account='oz_acc1' and captured_at::date='2026-08-20' group by 1,2")])
P('bids has 35269713/40385357', [dict(r) for r in db.query("select campaign_id, count(distinct captured_at) nd, min(captured_at) d0, max(captured_at) d1 from ozon_bids where account='oz_acc1' and campaign_id::text in ('35269713','40385357') group by 1")])
P('ads_daily campaigns after 20.08', db.query("select count(distinct campaign_id) n from mkt_ozon_ads_sku_daily where account='oz_acc1' and stat_date>='2026-08-20'")[0])
P('ads_daily 35269713/40385357', [dict(r) for r in db.query("select campaign_id, count(*) n, sum(money_spent) m from mkt_ozon_ads_sku_daily where account='oz_acc1' and campaign_id::text in ('35269713','40385357') and stat_date>='2026-08-20' group by 1")])
P('ozon_ads campaigns absent from bids after 20.08', [dict(r) for r in db.query("""select a.campaign_id::text c from (select distinct campaign_id from mkt_ozon_ads_sku_daily where account='oz_acc1' and stat_date>='2026-08-20') a
 where a.campaign_id::text not in (select distinct campaign_id::text from ozon_bids where account='oz_acc1' and captured_at>='2026-08-20') limit 20""")])
