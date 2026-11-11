-- поток: card. Не выдаём потерянную историю попыток за нулевую.
ALTER TABLE card_status ADD COLUMN IF NOT EXISTS product_created_at timestamptz;
ALTER TABLE card_status ADD COLUMN IF NOT EXISTS history_missing boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS card_recovery_control (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    restored_at timestamptz NOT NULL DEFAULT now(),
    legacy_once_enabled boolean NOT NULL DEFAULT false,
    legacy_daily_limit integer NOT NULL DEFAULT 20 CHECK(legacy_daily_limit BETWEEN 1 AND 20),
    paused_reason text
);
INSERT INTO card_recovery_control(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS card_scan_state (
    account text PRIMARY KEY,
    started_at timestamptz,
    completed_at timestamptz,
    success boolean NOT NULL DEFAULT false,
    error_kind text
);
ALTER TABLE card_push_log ADD COLUMN IF NOT EXISTS action_state text NOT NULL DEFAULT 'legacy';
ALTER TABLE card_push_log ADD COLUMN IF NOT EXISTS request_payload jsonb;
ALTER TABLE card_push_log ADD COLUMN IF NOT EXISTS baseline jsonb NOT NULL DEFAULT '{}';
ALTER TABLE card_push_log ADD COLUMN IF NOT EXISTS recovery_attempt boolean NOT NULL DEFAULT false;
ALTER TABLE card_push_log ADD COLUMN IF NOT EXISTS error_kind text;
