-- поток: rev — durable moderation, publication journal and shared LLM budget
BEGIN;
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS recovery_active_unanswered boolean;

ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS recovery_status_checked_at timestamptz;

CREATE TABLE IF NOT EXISTS rev_telegram_inbox (
        update_id bigint PRIMARY KEY, payload jsonb NOT NULL,
        state text NOT NULL DEFAULT 'pending', received_at timestamptz DEFAULT now(),
        handled_at timestamptz, error_type text);

CREATE TABLE IF NOT EXISTS rev_telegram_checkpoint (
        id int PRIMARY KEY CHECK(id=1), next_offset bigint NOT NULL);

CREATE TABLE IF NOT EXISTS rev_edit_session (
        user_id text PRIMARY KEY, mod_id bigint NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS rev_publication_attempt (
        platform text, account text, kind text, ext_id text,
        state text NOT NULL, final_text text NOT NULL, created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,account,kind,ext_id));

CREATE TABLE IF NOT EXISTS rev_llm_budget (
        request_key text PRIMARY KEY, day date NOT NULL,
        reserved_usd numeric NOT NULL CHECK(reserved_usd>=0),
        actual_usd numeric CHECK(actual_usd>=0), provider text NOT NULL,
        created_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS rev_search_cache (
        query_key text PRIMARY KEY, evidence jsonb NOT NULL, cached_at timestamptz DEFAULT now());

CREATE TABLE IF NOT EXISTS rev_catalog_refresh (
        channel text PRIMARY KEY, last_ok_at timestamptz, last_error text);
CREATE INDEX IF NOT EXISTS idx_rev_raw_wb_nm ON raw_wb_card_content(nm_id);
CREATE INDEX IF NOT EXISTS idx_rev_wb_cards_nm ON wb_cards(nm_id);
CREATE INDEX IF NOT EXISTS idx_rev_ozon_product_sku ON ozon_product(sku);
CREATE INDEX IF NOT EXISTS idx_rev_ozon_attributes_offer ON raw_ozon_attributes(offer_id);
COMMIT;
