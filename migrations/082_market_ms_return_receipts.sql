-- поток: fin
-- Linked MoySklad salesreturn documents for Market orders. A document is
-- evidence of receipt only when posted and linked to the original shipment.
CREATE TABLE IF NOT EXISTS market_ms_return_receipts (
    salesreturn_id uuid PRIMARY KEY,
    demand_id uuid NOT NULL,
    order_number text NOT NULL,
    account text NOT NULL,
    agent_id uuid NOT NULL,
    store_name text,
    applicable boolean NOT NULL,
    moment timestamp without time zone,
    ms_updated_at timestamp without time zone,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_ms_return_receipts_order_idx
    ON market_ms_return_receipts(account, order_number);

CREATE TABLE IF NOT EXISTS market_ms_return_scan (
    agent_id uuid PRIMARY KEY,
    earliest_scanned date NOT NULL,
    last_success_at timestamptz NOT NULL,
    documents_seen integer NOT NULL
);
