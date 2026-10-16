-- поток: ev. Official quantities are isolated from legacy snapshot/cost builders.
CREATE TABLE IF NOT EXISTS wb_loss_document (
 id text PRIMARY KEY, account text NOT NULL, warehouse text NOT NULL,
 source_archive text NOT NULL, source_member text NOT NULL, pdf_name text NOT NULL,
 header_date date, pdf bytea NOT NULL, signature_present boolean NOT NULL,
 imported_at timestamptz NOT NULL DEFAULT now(), UNIQUE(account,warehouse)
);
CREATE TABLE IF NOT EXISTS wb_loss_item (
 document_id text REFERENCES wb_loss_document(id), nm_id bigint NOT NULL,
 vendor_code text NOT NULL, qty integer NOT NULL CHECK(qty>0), source_pages integer[] NOT NULL,
 PRIMARY KEY(document_id,nm_id)
);
CREATE TABLE IF NOT EXISTS wb_loss_order_raw (
 account text NOT NULL, order_id bigint NOT NULL, source text NOT NULL,
 payload jsonb NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account,order_id,source)
);
CREATE TABLE IF NOT EXISTS wb_loss_search_page (
 account text NOT NULL, source text NOT NULL, cursor text NOT NULL,
 payload jsonb NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account,source,cursor)
);
CREATE TABLE IF NOT EXISTS wb_loss_analysis (
 account text PRIMARY KEY, payload jsonb NOT NULL, built_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wb_loss_ms_lookup (
 account text NOT NULL, order_id text NOT NULL, payload jsonb NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account,order_id)
);
