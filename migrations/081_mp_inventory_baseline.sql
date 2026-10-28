-- поток: fin. Immutable starting point for observing marketplace-stock movement.
-- This date is the first confirmed daily capture available when the user requested the monitor.
CREATE TABLE IF NOT EXISTS mp_inventory_baseline_capture (
  platform text NOT NULL,
  account text NOT NULL,
  captured_at date NOT NULL,
  source_recorded_at timestamptz NOT NULL,
  source_rows integer NOT NULL,
  usable_available numeric NOT NULL,
  frozen_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(platform,account)
);
CREATE TABLE IF NOT EXISTS mp_inventory_baseline_item (
  platform text NOT NULL,
  account text NOT NULL,
  sku text NOT NULL,
  warehouse text NOT NULL,
  usable_available numeric NOT NULL,
  raw_available numeric NOT NULL,
  excluded_loss numeric NOT NULL,
  PRIMARY KEY(platform,account,sku,warehouse)
);
INSERT INTO mp_inventory_baseline_capture
  (platform,account,captured_at,source_recorded_at,source_rows,usable_available)
SELECT platform,account,captured_at,recorded_at,source_rows,usable_available
FROM mp_stock_capture WHERE captured_at='2026-10-28'
ON CONFLICT DO NOTHING;
INSERT INTO mp_inventory_baseline_item
  (platform,account,sku,warehouse,usable_available,raw_available,excluded_loss)
SELECT h.platform,h.account,h.sku,h.warehouse,h.usable_available,h.raw_available,h.excluded_loss
FROM mp_stock_history h JOIN mp_inventory_baseline_capture c
  ON c.platform=h.platform AND c.account=h.account AND c.captured_at=h.captured_at
ON CONFLICT DO NOTHING;
