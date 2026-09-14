# read-only pull: SELECT only -> pickles in bundles/data
import sys, os, re
sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
from q import conn
import pandas as pd
D='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/data'
os.makedirs(D, exist_ok=True)
c=conn()
def df(sql, name):
    assert re.match(r'^\s*(select|with)', sql, re.I)
    x=pd.read_sql(sql, c); x.to_pickle(f'{D}/{name}.pkl'); print(name, len(x)); return x

prod=df("select account, sku::text sku, offer_id, name, is_archived from ozon_product where account in ('oz_acc1','oz_acc2')", 'prod')

df("""select p.account, p.posting_number, (p.in_process_at at time zone 'Europe/Moscow')::date d, p.status, p.scheme,
   e->>'sku' sku, e->>'offer_id' offer_id, (e->>'price')::numeric price, (e->>'quantity')::int qty
   from raw_ozon_posting p, jsonb_array_elements(p.payload->'products') e
   where p.account in ('oz_acc1','oz_acc2')""", 'post')

df("select stat_date d, account, campaign_id::text campaign_id, sku::text sku, bid, views, clicks, money_spent, orders_qty, orders_money from mkt_ozon_ads_sku_daily", 'ads')

df("""select account, campaign_id::text campaign_id, max(campaign_title) title, sku::text sku, min(captured_at)::date first_d, max(captured_at)::date last_d, count(distinct captured_at::date) ndays
   from ozon_bids where captured_at >= '2026-08-07' group by 1,2,4""", 'bids')

df("""select t.account, t.payload->>'operation_id' op, (t.payload->>'operation_date')::date d, t.payload->>'operation_type' optype,
   coalesce((t.payload->>'accruals_for_sale')::numeric,0) accr, coalesce((t.payload->>'sale_commission')::numeric,0) comm,
   coalesce((t.payload->>'amount')::numeric,0) amount,
   coalesce((t.payload->>'delivery_charge')::numeric,0)+coalesce((t.payload->>'return_delivery_charge')::numeric,0) deliv,
   (select coalesce(sum((s->>'price')::numeric),0) from jsonb_array_elements(coalesce(t.payload->'services','[]'::jsonb)) s) serv,
   t.payload->'posting'->>'posting_number' posting_number,
   i.value->>'sku' sku, jsonb_array_length(t.payload->'items') nitems
   from raw_ozon_transaction t, jsonb_array_elements(t.payload->'items') i
   where t.account in ('oz_acc1','oz_acc2') and (t.payload->>'operation_date')::date >= '2026-05-01'
     and jsonb_array_length(coalesce(t.payload->'items','[]'::jsonb))>0""", 'tx')

bases = sorted(set(re.sub(r'X(2|4|6|8|10)$','',o) for o in prod.offer_id.dropna() if re.search(r'X(2|4|6|8|10)$', o)))
inlist = "','".join(b.replace("'","") for b in bases)
df(f"""select captured_at::date d, external_code, sum(greatest(coalesce(stock,0)-coalesce(reserve,0),0)) avail,
   percentile_cont(0.5) within group (order by cost_seb) filter (where cost_seb>0) cost_seb_med, count(*) nrows
   from supplier_stock where external_code in ('{inlist}') and captured_at>='2026-06-22' group by 1,2""", 'stock')
df(f"select external_code, cost_seb, buy_price from products where external_code in ('{inlist}')", 'products_cost')
df(f"select external_code, buy_price, archived from ms_product where external_code in ('{inlist}') or external_code ~ 'X(2|4|6|8|10)$'", 'ms_cost')
df("""select i.account, i.sku::text sku, i.offer_id, i.qty, i.price, (r.created_at at time zone 'Europe/Moscow')::date d
   from mp_return_items i join mp_returns r on r.platform=i.platform and r.account=i.account and r.return_id=i.return_id
   where i.account in ('oz_acc1','oz_acc2')""", 'ret')
df("""select account, sku::text sku, period_start, period_end, unique_search_users, unique_view_users, order_count, gmv, position
   from ozon_search_product where period_end - period_start = 7 and period_start >= '2026-07-06'""", 'search')
df("""select account, sku::text sku,
   avg(price) filter (where collected_on between '2026-08-25' and '2026-09-01') p_before,
   avg(price) filter (where collected_on between '2026-09-05' and '2026-09-10') p_after,
   avg(marketing_price) filter (where collected_on between '2026-08-08' and '2026-09-13') mp_avg,
   avg(commission_fbs_pct) filter (where collected_on between '2026-08-08' and '2026-09-13') comm_fbs
   from ozon_price_index where collected_on>='2026-08-08' group by 1,2""", 'price')
df("select article sku, account, period_from, revenue_buyer, cogs, commission, logistics from margin_by_sku where platform='ozon' and period_from>='2026-05-01'", 'mbs')
