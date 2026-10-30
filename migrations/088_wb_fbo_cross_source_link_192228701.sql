-- поток: fin. A cross-API stock/order match is an inference, not a WB movement ID or COGS proof.
CREATE TABLE IF NOT EXISTS mp_fbo_cross_source_link_candidates (
  id bigserial PRIMARY KEY,
  account text NOT NULL,
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  sku text NOT NULL,
  source_warehouse text NOT NULL,
  previous_capture date NOT NULL,
  current_capture_id bigint NOT NULL REFERENCES wb_fbo_intraday_capture(id),
  quantity numeric NOT NULL CHECK (quantity > 0),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id,sku,previous_capture,current_capture_id)
);
CREATE INDEX IF NOT EXISTS mp_fbo_cross_source_link_lookup
  ON mp_fbo_cross_source_link_candidates(account,sku,source_warehouse);

WITH previous AS (
  SELECT h.captured_at,h.recorded_at,h.raw_available,h.excluded_loss,
         h.usable_available,h.in_way_to_client
  FROM mp_stock_history h JOIN mp_stock_capture c
    ON c.platform=h.platform AND c.account=h.account AND c.captured_at=h.captured_at
  WHERE h.platform='wb' AND h.account='wb_acc1' AND h.sku='192228701'
    AND h.warehouse='Склад WB РФ' AND h.captured_at='2026-10-30'
    AND c.source_rows>0
), current AS (
  SELECT c.id,c.completed_at,c.source_rows,
         sum(i.quantity) quantity,sum(i.in_way_to_client) in_way_to_client
  FROM wb_fbo_intraday_capture c JOIN wb_fbo_intraday_item i ON i.capture_id=c.id
  WHERE c.account='wb_acc1' AND c.completed_at>='2026-10-30 14:00:00+00'
    AND c.completed_at<'2026-10-30 14:15:00+00' AND i.nm_id=192228701
  GROUP BY c.id,c.completed_at,c.source_rows
), matching_orders AS (
  SELECT o.id,o.posting_number,o.created_at,o.products
  FROM mp_fbo_orders o CROSS JOIN previous p CROSS JOIN current c
  WHERE o.platform='wb' AND o.account='wb_acc1'
    AND o.created_at>p.recorded_at AND o.created_at<=c.completed_at
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(o.products) product
                WHERE product->>'sku'='192228701')
), unique_order AS (
  SELECT * FROM matching_orders WHERE (SELECT count(*) FROM matching_orders)=1
    AND posting_number='eAh.dba25db50b5664013972de244359d2742.0.0'
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(products) product
                WHERE product->>'sku'='192228701' AND product->>'quantity'='1')
)
INSERT INTO mp_fbo_cross_source_link_candidates
  (account,order_id,sku,source_warehouse,previous_capture,current_capture_id,quantity,evidence)
SELECT 'wb_acc1',o.id,'192228701','Склад WB РФ',p.captured_at,c.id,1,
  jsonb_build_object('kind','unique_order_cross_source_stock_out',
    'confidence','inferred_cross_source','wb_movement_id',NULL,'cogs_assigned',false,
    'previous_source','warehouse_remains','current_source','stocks-report/wb-warehouses',
    'previous_observed_at',p.recorded_at,'current_observed_at',c.completed_at,
    'order_posting_number',o.posting_number,'order_created_at',o.created_at,
    'stock_before',p.usable_available,'stock_after',c.quantity,
    'in_way_to_client_before',p.in_way_to_client,
    'in_way_to_client_after',c.in_way_to_client)
FROM unique_order o CROSS JOIN previous p CROSS JOIN current c
WHERE p.raw_available=1 AND p.excluded_loss=0 AND p.usable_available=1
  AND coalesce(p.in_way_to_client,0)=0 AND c.quantity=0 AND c.in_way_to_client=1
  AND c.source_rows>0
ON CONFLICT DO NOTHING;
