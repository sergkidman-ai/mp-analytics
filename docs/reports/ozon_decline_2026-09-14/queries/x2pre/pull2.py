# read-only: SELECT -> pickles (no writes anywhere except scratchpad)
import sys,re; sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
from q import conn
import pandas as pd
D='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
c=conn()
def df(sql,name):
    assert re.match(r'^\s*(select|with)',sql,re.I); x=pd.read_sql(sql,c); x.to_pickle(D+name+'.pkl'); print(name,len(x))
df("select captured_at d, campaign_id, campaign_title title, sku, bid, state, adv_type from ozon_bids where account='oz_acc1' and captured_at>='2026-08-07'",'bids_acc1')
df("select sku::text sku, sum(free_to_sell) fbo_free from ozon_fbo_stock where account='oz_acc1' and captured_at=(select max(captured_at) from ozon_fbo_stock where account='oz_acc1') group by 1",'fbo_last')
df("select sku::text sku, offer_id, price, marketing_seller_price, marketing_price, commission_fbs_pct, acquiring, collected_on from ozon_price_index where account='oz_acc1' and collected_on>=current_date-3",'price_last')
df("select captured_at::date d, external_code, sum(greatest(coalesce(stock,0)-coalesce(reserve,0),0)) avail, percentile_cont(0.5) within group (order by cost_seb) filter (where cost_seb>0) cost_seb_med from supplier_stock where captured_at>='2026-08-15' group by 1,2",'sstock')
