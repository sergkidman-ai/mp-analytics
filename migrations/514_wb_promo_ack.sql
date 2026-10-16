-- поток: ev — подтверждение и ежечасные напоминания по новой автоакции WB
CREATE TABLE IF NOT EXISTS wb_promo_task (
    id bigserial PRIMARY KEY,
    group_key text NOT NULL UNIQUE,
    name text NOT NULL,
    starts_on date NOT NULL,
    ends_on date,
    accounts text[] NOT NULL,
    promo_refs jsonb NOT NULL,
    recipient_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_sent_at timestamptz,
    last_message_id bigint,
    send_count integer NOT NULL DEFAULT 0,
    done_at timestamptz,
    done_by bigint
);
CREATE INDEX IF NOT EXISTS wb_promo_task_pending_idx
    ON wb_promo_task(last_sent_at) WHERE done_at IS NULL;
CREATE TABLE IF NOT EXISTS prc_promo_callback_cursor (
    id integer PRIMARY KEY CHECK (id=1),
    next_update_id bigint NOT NULL DEFAULT 0
);
INSERT INTO prc_promo_callback_cursor(id) VALUES (1) ON CONFLICT DO NOTHING;
