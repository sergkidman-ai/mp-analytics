-- поток: fin. Confirm the existing internal WB FBO link by a unique observed match.
-- This establishes an order/stock-out relation, not a WB movement ID or unit COGS.
WITH previous_capture AS (
  SELECT recorded_at FROM mp_stock_capture
  WHERE platform='wb' AND account='wb_acc1' AND captured_at='2026-10-27'
    AND source_rows>0
), current_capture AS (
  SELECT recorded_at FROM mp_stock_capture
  WHERE platform='wb' AND account='wb_acc1' AND captured_at='2026-10-28'
    AND source_rows>0
), matching_orders AS (
  SELECT o.id,o.posting_number,o.created_at,o.products
  FROM mp_fbo_orders o CROSS JOIN previous_capture p CROSS JOIN current_capture c
  WHERE o.platform='wb' AND o.account='wb_acc1'
    AND o.created_at>p.recorded_at AND o.created_at<=c.recorded_at
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(o.products) product
                WHERE product->>'sku'='199573317')
), unique_order AS (
  SELECT * FROM matching_orders WHERE (SELECT count(*) FROM matching_orders)=1
    AND EXISTS (SELECT 1 FROM jsonb_array_elements(products) product
                WHERE product->>'sku'='199573317' AND product->>'quantity'='1')
), proven_link AS (
  SELECT l.id FROM mp_fbo_stock_link_candidates l
  JOIN unique_order o ON o.id=l.order_id
  JOIN mp_stock_history s ON s.platform='wb' AND s.account=l.account
    AND s.sku=l.sku AND s.warehouse=l.source_warehouse
    AND s.captured_at=l.previous_capture
  JOIN mp_stock_history t ON t.platform='wb' AND t.account=l.account
    AND t.sku=l.sku AND t.warehouse='(в пути)'
    AND t.captured_at=l.current_capture
  WHERE l.account='wb_acc1' AND l.sku='199573317'
    AND l.source_warehouse='Склад WB РФ'
    AND l.previous_capture='2026-10-27' AND l.current_capture='2026-10-28'
    AND l.quantity=1 AND l.evidence->>'confidence'='candidate'
    AND s.usable_available=1 AND s.raw_available=1 AND s.excluded_loss=0
    AND coalesce(s.in_way_to_client,0)=0 AND t.in_way_to_client=1
    AND NOT EXISTS (
      SELECT 1 FROM mp_stock_history h WHERE h.platform='wb' AND h.account=l.account
        AND h.sku=l.sku AND h.captured_at=l.current_capture
        AND h.usable_available>0
    )
)
UPDATE mp_fbo_stock_link_candidates l SET evidence=l.evidence ||
  jsonb_build_object('confidence','inferred_unique','wb_movement_id',NULL,
    'cogs_assigned',false,'link_rule','unique_one_unit_stock_out_and_fbo_order_between_captures')
FROM proven_link p WHERE l.id=p.id;
