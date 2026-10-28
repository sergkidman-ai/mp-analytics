-- поток: fin. FBO stock evidence; no inferred unit cost or order attribution.
CREATE OR REPLACE VIEW wb_stocks_usable AS
SELECT s.*,
       LEAST(GREATEST(COALESCE(s.quantity,0),0),COALESCE(l.qty,0)) AS excluded_loss,
       GREATEST(COALESCE(s.quantity,0)-COALESCE(l.qty,0),0) AS usable_quantity,
       GREATEST(COALESCE(s.quantity_full,0)-COALESCE(l.qty,0),0) AS usable_quantity_full
FROM wb_stocks s
LEFT JOIN LATERAL (
  SELECT sum(i.qty) qty
  FROM wb_loss_document d JOIN wb_loss_item i ON i.document_id=d.id
  WHERE d.account=s.account AND d.warehouse=s.warehouse AND i.nm_id=s.nm_id
    AND (d.header_date IS NULL OR s.captured_at>=d.header_date)
) l ON true;

CREATE TABLE IF NOT EXISTS mp_stock_history (
  platform text NOT NULL CHECK (platform IN ('wb','ozon')),
  account text NOT NULL,
  sku text NOT NULL,
  warehouse text NOT NULL,
  captured_at date NOT NULL,
  source_table text NOT NULL,
  raw_available numeric NOT NULL,
  excluded_loss numeric NOT NULL DEFAULT 0,
  usable_available numeric NOT NULL,
  raw_total numeric,
  usable_total numeric,
  reserved numeric,
  promised numeric,
  in_way_to_client numeric,
  in_way_from_client numeric,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(platform,account,sku,warehouse,captured_at),
  CHECK(excluded_loss>=0 AND usable_available>=0)
);
CREATE INDEX IF NOT EXISTS mp_stock_history_date_idx ON mp_stock_history(platform,account,captured_at);

CREATE TABLE IF NOT EXISTS mp_stock_capture (
  platform text NOT NULL CHECK (platform IN ('wb','ozon')),
  account text NOT NULL,
  captured_at date NOT NULL,
  source_rows integer NOT NULL CHECK(source_rows>=0),
  raw_available numeric NOT NULL,
  excluded_loss numeric NOT NULL,
  usable_available numeric NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(platform,account,captured_at)
);
