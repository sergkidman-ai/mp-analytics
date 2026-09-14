import sys, os, time
sys.path.insert(0, '/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
import pandas as pd
from q import conn
D = os.path.dirname(os.path.abspath(__file__)) + '/cache'
os.makedirs(D, exist_ok=True)
Q = {
'post_lines': """select p.account, (p.in_process_at at time zone 'Europe/Moscow')::date as day, p.posting_number, p.status, p.scheme,
  e->>'sku' sku, e->>'offer_id' offer_id, left(e->>'name',120) name, (e->>'price')::numeric price, (e->>'quantity')::numeric qty
  from raw_ozon_posting p, jsonb_array_elements(p.payload->'products') e""",
'tx_daily': """select account, (payload->>'operation_date')::date as day,
  sum(greatest((payload->>'accruals_for_sale')::numeric,0)) accr_pos,
  sum(least((payload->>'accruals_for_sale')::numeric,0)) accr_neg,
  sum((payload->>'amount')::numeric) saldo,
  sum((payload->>'amount')::numeric) filter (where jsonb_array_length(coalesce(payload->'items','[]'::jsonb))=0) saldo_noitems,
  count(distinct payload->'posting'->>'posting_number') filter (where (payload->>'accruals_for_sale')::numeric>0) n_sold_posts
  from raw_ozon_transaction where (payload->>'operation_date')>='2026-03-01' group by 1,2""",
'tx_sku_daily': """select account, (payload->>'operation_date')::date as day, it->>'sku' sku,
  sum(greatest((payload->>'accruals_for_sale')::numeric,0)/n) accr_pos,
  sum(least((payload->>'accruals_for_sale')::numeric,0)/n) accr_neg,
  sum((payload->>'amount')::numeric/n) saldo
  from raw_ozon_transaction t, lateral (select jsonb_array_length(payload->'items') n) nn, jsonb_array_elements(payload->'items') it
  where (payload->>'operation_date')>='2026-03-01' and nn.n>0 group by 1,2,3""",
'margin': """select account, article sku, period_from, revenue_buyer, cogs from margin_by_sku where platform='ozon' and period_from>='2026-03-01'""",
'ad_daily': """select account, date as day, spend from ad_spend_daily where platform='ozon'""",
'ads_month': """select account, period, covered_to, sum(spend) spend, sum(views) views, sum(clicks) clicks, sum(orders) orders, sum(sold) sold, sum(ad_revenue) ad_revenue from ozon_ads group by 1,2,3""",
'ads_sku': """select account, stat_date as day, sku, max(offer_id) offer_id, sum(views) views, sum(clicks) clicks, sum(money_spent) spend, sum(orders_qty) orders, sum(orders_money) orders_money
  from mkt_ozon_ads_sku_daily group by 1,2,3""",
'search': """select account, period_start, period_end, sku, offer_id, left(name,120) name, position, unique_search_users, unique_view_users, order_count, gmv
  from ozon_search_product where period_end - period_start = 7""",
'stock': """select captured_at as day, external_code, sum(stock) stock, sum(coalesce(reserve,0)) reserve from supplier_stock where stock>0 group by 1,2""",
'fbo': """select captured_at as day, account, sku, sum(free_to_sell) fts from ozon_fbo_stock group by 1,2,3""",
'returns': """select r.account, (r.created_at at time zone 'Europe/Moscow')::date as day, r.return_id, r.scheme, r.amount, i.sku, i.offer_id, i.qty, i.price
  from mp_returns r left join mp_return_items i using (platform, account, return_id) where r.platform='ozon'""",
'product': """select account, sku, offer_id, left(name,150) name, is_archived from ozon_product""",
}
only = sys.argv[1:]
with conn() as c:
    for k, sql in Q.items():
        if only and k not in only: continue
        t = time.time()
        df = pd.read_sql(sql, c)
        df.to_pickle(f'{D}/{k}.pkl')
        print(k, len(df), round(time.time() - t, 1), 's')
