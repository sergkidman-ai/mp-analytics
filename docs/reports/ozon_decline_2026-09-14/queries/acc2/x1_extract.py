# acc2 decline: дополнительные выгрузки (read-only). Кэш в acc2/cache
import sys, os, time
sys.path.insert(0, '/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
import pandas as pd
from q import conn
D = os.path.dirname(os.path.abspath(__file__)) + '/cache'
os.makedirs(D, exist_ok=True)
Q = {
'real': """select account, year, month, x->'item'->>'sku' sku, x->'item'->>'offer_id' offer_id, left(x->'item'->>'name',150) name,
  (x->>'seller_price_per_instance')::numeric price,
  coalesce((x->'delivery_commission'->>'quantity')::numeric,0) dq, coalesce((x->'return_commission'->>'quantity')::numeric,0) rq
  from raw_ozon_realization, jsonb_array_elements(payload->'rows') x where year=2026 and month between 1 and 8""",
'tx_units': """select account, (payload->>'operation_date')::date as day, payload->>'operation_type' op, it->>'sku' sku,
  count(*) units, sum((payload->>'accruals_for_sale')::numeric/n) accr
  from raw_ozon_transaction t, lateral (select jsonb_array_length(payload->'items') n) nn, jsonb_array_elements(payload->'items') it
  where payload->>'operation_date'>='2026-03-01' and nn.n>0
    and payload->>'operation_type' in ('OperationAgentDeliveredToCustomer','ClientReturnAgentOperation') group by 1,2,3,4""",
'tx_types': """select account, left(payload->>'operation_date',10)::date as day, payload->>'operation_type' op, max(payload->>'operation_type_name') opname,
  count(*) n, sum((payload->>'amount')::numeric) amount, sum((payload->>'accruals_for_sale')::numeric) accr
  from raw_ozon_transaction where payload->>'operation_date'>='2026-03-01' group by 1,2,3""",
'signals': """select captured_at as day, sku, offer_id, days_without_sales, idc, ads from ozon_stock_signals where account='oz_acc2'""",
'pidx2': """select sku, offer_id, collected_on, price, marketing_price, external_index, color_index from ozon_price_index where account='oz_acc2'""",
'pidx1_first': """select sku, offer_id, min(collected_on) first_on, max(collected_on) last_on, count(*) n from ozon_price_index where account='oz_acc1' group by 1,2""",
'actions': """select ts, account, action_id, action_title, offer_id, action_price, stock, rung, ok, left(note,80) note from oz_action_log""",
'product2': """select account, sku, offer_id, left(name,150) name, is_archived, updated_at from ozon_product""",
'card_status': """select account, offer_id, err_class, status, is_selling, first_seen, last_seen from card_status where platform='ozon'""",
'attrs_first': """select account, offer_id, sku, min(collected_at) first_at, max(collected_at) last_at from raw_ozon_attributes group by 1,2,3""",
'bids': """select captured_at as day, account, count(distinct sku) n_sku, count(*) n_links from ozon_bids group by 1,2""",
'bids_sku2': """select captured_at as day, sku from ozon_bids where account='oz_acc2' and captured_at in ('2026-06-23','2026-07-17','2026-08-07','2026-08-17','2026-08-19','2026-09-01','2026-09-13','2026-09-14') group by 1,2""",
'search_hdr': """select account, period_start, count(*) n, sum(unique_view_users) views, sum(unique_search_users) demand, sum(order_count) orders, sum(gmv) gmv from ozon_search_product where period_end-period_start=7 group by 1,2""",
}
only = sys.argv[1:]
with conn() as c:
    for k, sql in Q.items():
        if only and k not in only: continue
        t = time.time()
        df = pd.read_sql(sql, c)
        df.to_pickle(f'{D}/{k}.pkl')
        print(k, len(df), round(time.time() - t, 1), 's')
