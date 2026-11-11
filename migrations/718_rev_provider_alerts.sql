-- поток: rev
CREATE TABLE IF NOT EXISTS rev_batch_health (
    provider text PRIMARY KEY, reason text NOT NULL, detail text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(), retry_at timestamptz NOT NULL);
ALTER TABLE rev_batch_health ADD COLUMN IF NOT EXISTS severity text NOT NULL DEFAULT 'critical';
ALTER TABLE rev_batch_health ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'request';
ALTER TABLE rev_batch_health ADD COLUMN IF NOT EXISTS blocks_work boolean NOT NULL DEFAULT true;
ALTER TABLE rev_batch_health ADD COLUMN IF NOT EXISTS failures integer NOT NULL DEFAULT 1;
ALTER TABLE rev_batch_health ADD COLUMN IF NOT EXISTS last_event_at timestamptz;
CREATE TABLE IF NOT EXISTS rev_provider_alert (
    id bigserial PRIMARY KEY, provider text NOT NULL, kind text NOT NULL,
    reason text NOT NULL, severity text NOT NULL, message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), delivered_at timestamptz,
    message_id bigint, last_error text, superseded_at timestamptz);
CREATE INDEX IF NOT EXISTS idx_rev_provider_alert_pending ON rev_provider_alert(id)
    WHERE delivered_at IS NULL AND superseded_at IS NULL;
