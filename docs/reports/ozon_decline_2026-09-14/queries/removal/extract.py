# read-only extraction -> pickles in removal/data
import sys, os, time
sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
from q import conn
import pandas as pd
D='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal/data'
os.makedirs(D,exist_ok=True)
Q={
'bids': """select account, captured_at, campaign_id, campaign_title, sku, bid, state from ozon_bids
           where captured_at>='2026-08-01'""",
'ads': """select account, stat_date, campaign_id, sku, offer_id, views, clicks, money_spent spend, orders_qty, orders_money
          from mkt_ozon_ads_sku_daily where stat_date>='2026-07-27'""",
'post': """select p.account, (p.in_process_at at time zone 'Europe/Moscow')::date d, p.posting_number, p.scheme, p.status,
           e->>'sku' sku, e->>'offer_id' offer_id, (e->>'price')::numeric price, (e->>'quantity')::numeric qty
           from raw_ozon_posting p, jsonb_array_elements(p.payload->'products') e
           where p.in_process_at >= '2026-06-20'""",
'price': """select account, collected_on d, sku, offer_id, price, marketing_price, auto_action_enabled from ozon_price_index where account='oz_acc1'""",
'ss': """select captured_at d, external_code, sum(greatest(coalesce(stock,0)-coalesce(reserve,0),0)) free, sum(coalesce(stock,0)) stock
         from supplier_stock where captured_at>='2026-07-20' and external_code is not null group by 1,2""",
'fbo': """select account, captured_at d, sku, sum(free_to_sell) fts from ozon_fbo_stock where captured_at>='2026-07-20' group by 1,2,3""",
'search': """select account, period_start, period_end, sku, position, unique_search_users, unique_view_users, order_count, gmv
             from ozon_search_product where period_end-period_start=7 and period_start>='2026-06-22'""",
'tx': """with t as (select account, payload p from raw_ozon_transaction
            where (payload->>'operation_date')::date between '2026-07-20' and '2026-09-08')
         select account, (p->>'operation_date')::date d, p->>'operation_type' optype, (p->>'amount')::numeric amount,
                (p->>'accruals_for_sale')::numeric accr, jsonb_array_length(coalesce(p->'items','[]'::jsonb)) nitems,
                i->>'sku' sku
         from t left join lateral jsonb_array_elements(coalesce(p->'items','[]'::jsonb)) i on true""",
'mbs': """select account, article sku, period_from, revenue_buyer, cogs, net_profit from margin_by_sku
          where platform='ozon' and period_from>='2026-07-01'""",
'prod': """select account, sku, offer_id, name, is_archived from ozon_product""",
'act': """select account, ts, action_title, offer_id, action_price, ok, note from oz_action_log""",
'card': """select account, offer_id, attempts, first_seen, last_attempt_at, healed_at, err_class from card_status where platform='ozon'""",
'sig': """select account, captured_at d, sku, offer_id, idc, ads from ozon_stock_signals where captured_at>='2026-07-20'""",
}
only=sys.argv[1:] or list(Q)
with conn() as c:
    for k in only:
        t=time.time()
        df=pd.read_sql(Q[k],c)
        df.to_pickle(f'{D}/{k}.pkl')
        print(k,len(df),round(time.time()-t,1),'s')
