-- поток: fin. A stock-delta/order match is a candidate, not a traced WB unit or COGS proof.
CREATE TABLE IF NOT EXISTS mp_fbo_stock_link_candidates (
  id bigserial PRIMARY KEY,
  account text NOT NULL,
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  sku text NOT NULL,
  source_warehouse text NOT NULL,
  previous_capture date NOT NULL,
  current_capture date NOT NULL,
  quantity numeric NOT NULL CHECK (quantity > 0),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (previous_capture < current_capture),
  UNIQUE (account, sku, source_warehouse, previous_capture, current_capture),
  UNIQUE (order_id, sku, previous_capture, current_capture)
);

-- The only WB acc1 order for this nmID/quantity in the observed interval.
-- Stock 1 -> 0 and in-way-to-client 0 -> 1; no WB movement/order ID is available.
WITH prior AS (
  SELECT usable_available,recorded_at
  FROM mp_stock_history
  WHERE platform='wb' AND account='wb_acc1' AND sku='199573317'
    AND warehouse='Склад WB РФ' AND captured_at='2026-10-27'
), transit_before AS (
  SELECT in_way_to_client FROM mp_stock_history
  WHERE platform='wb' AND account='wb_acc1' AND sku='199573317'
    AND warehouse='Склад WB РФ' AND captured_at='2026-10-27'
), transit_after AS (
  SELECT in_way_to_client,recorded_at FROM mp_stock_history
  WHERE platform='wb' AND account='wb_acc1' AND sku='199573317'
    AND warehouse='(в пути)' AND captured_at='2026-10-28'
), matching_orders AS (
  SELECT o.id,o.created_at,o.posting_number
  FROM mp_fbo_orders o
  WHERE o.platform='wb' AND o.account='wb_acc1'
    AND o.created_at>(SELECT recorded_at FROM prior)
    AND o.created_at<=(SELECT recorded_at FROM transit_after)
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(o.products) p
                WHERE p->>'sku'='199573317' AND p->>'quantity'='1')
), one_order AS (
  SELECT * FROM matching_orders WHERE (SELECT count(*) FROM matching_orders)=1
)
INSERT INTO mp_fbo_stock_link_candidates
  (account,order_id,sku,source_warehouse,previous_capture,current_capture,quantity,evidence)
SELECT 'wb_acc1',o.id,'199573317','Склад WB РФ','2026-10-27','2026-10-28',1,
  jsonb_build_object('kind','unique_sku_quantity_interval_and_in_way','confidence','candidate',
    'order_posting_number',o.posting_number,'order_created_at',o.created_at,
    'stock_before',p.usable_available,'stock_after',0,
    'in_way_to_client_before',b.in_way_to_client,
    'in_way_to_client_after',a.in_way_to_client,
    'previous_recorded_at',p.recorded_at,'current_recorded_at',a.recorded_at,
    'cogs_assigned',false)
FROM one_order o CROSS JOIN prior p CROSS JOIN transit_before b CROSS JOIN transit_after a
WHERE p.usable_available=1 AND b.in_way_to_client=0 AND a.in_way_to_client=1
  AND EXISTS (SELECT 1 FROM mp_stock_capture c WHERE c.platform='wb'
              AND c.account='wb_acc1' AND c.captured_at='2026-10-27' AND c.source_rows>0)
  AND EXISTS (SELECT 1 FROM mp_stock_capture c WHERE c.platform='wb'
              AND c.account='wb_acc1' AND c.captured_at='2026-10-28' AND c.source_rows>0)
  AND NOT EXISTS (
    SELECT 1 FROM mp_stock_history h
    WHERE h.platform='wb' AND h.account='wb_acc1' AND h.sku='199573317'
      AND h.warehouse='Склад WB РФ' AND h.captured_at='2026-10-28'
      AND h.usable_available<>0
  )
ON CONFLICT DO NOTHING;
