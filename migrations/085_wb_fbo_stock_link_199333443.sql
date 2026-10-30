-- поток: fin. Internally inferred WB FBO stock-out link; no WB movement ID or COGS claim.
-- The unique one-unit FBO order must fall between two complete stock captures.
WITH previous_capture AS (
  SELECT recorded_at FROM mp_stock_capture
  WHERE platform='wb' AND account='wb_acc1' AND captured_at='2026-10-28'
    AND source_rows>0
), current_capture AS (
  SELECT recorded_at FROM mp_stock_capture
  WHERE platform='wb' AND account='wb_acc1' AND captured_at='2026-10-29'
    AND source_rows>0
), previous_stock AS (
  SELECT usable_available,raw_available,excluded_loss,in_way_to_client
  FROM mp_stock_history
  WHERE platform='wb' AND account='wb_acc1' AND sku='199333443'
    AND warehouse='Склад WB РФ' AND captured_at='2026-10-28'
), current_transit AS (
  SELECT in_way_to_client FROM mp_stock_history
  WHERE platform='wb' AND account='wb_acc1' AND sku='199333443'
    AND warehouse='(в пути)' AND captured_at='2026-10-29'
), matching_orders AS (
  SELECT o.id,o.posting_number,o.created_at,o.products
  FROM mp_fbo_orders o CROSS JOIN previous_capture p CROSS JOIN current_capture c
  WHERE o.platform='wb' AND o.account='wb_acc1'
    AND o.created_at>p.recorded_at AND o.created_at<=c.recorded_at
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(o.products) product
                WHERE product->>'sku'='199333443')
), unique_order AS (
  SELECT * FROM matching_orders WHERE (SELECT count(*) FROM matching_orders)=1
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(products) product
                WHERE product->>'sku'='199333443' AND product->>'quantity'='1')
)
INSERT INTO mp_fbo_stock_link_candidates
  (account,order_id,sku,source_warehouse,previous_capture,current_capture,quantity,evidence)
SELECT 'wb_acc1',o.id,'199333443','Склад WB РФ','2026-10-28','2026-10-29',1,
  jsonb_build_object('kind','unique_sku_quantity_interval_and_in_way',
    'confidence','inferred_unique','wb_movement_id',NULL,'cogs_assigned',false,
    'order_posting_number',o.posting_number,'order_created_at',o.created_at,
    'stock_before',s.usable_available,'stock_after',0,
    'in_way_to_client_before',s.in_way_to_client,
    'in_way_to_client_after',t.in_way_to_client,
    'previous_recorded_at',p.recorded_at,'current_recorded_at',c.recorded_at)
FROM unique_order o CROSS JOIN previous_capture p CROSS JOIN current_capture c
CROSS JOIN previous_stock s CROSS JOIN current_transit t
WHERE s.usable_available=1 AND s.raw_available=1 AND s.excluded_loss=0
  AND coalesce(s.in_way_to_client,0)=0 AND t.in_way_to_client=1
  AND NOT EXISTS (
    SELECT 1 FROM mp_stock_history h WHERE h.platform='wb' AND h.account='wb_acc1'
      AND h.sku='199333443' AND h.captured_at='2026-10-29'
      AND h.usable_available>0
  )
ON CONFLICT DO NOTHING;
