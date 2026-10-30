-- поток: fin
-- Independent delivery record for additional loss-alert recipients.
BEGIN;
CREATE TABLE IF NOT EXISTS customer_guard_extra_delivery (
    incident_id bigint NOT NULL REFERENCES customer_guard_incidents(id),
    recipient text NOT NULL,
    delivery text NOT NULL CHECK (delivery IN ('sending','sent','failed','uncertain','suppressed')),
    attempted_at timestamptz NOT NULL,
    telegram_message_id bigint,
    delivery_error text,
    PRIMARY KEY (incident_id, recipient)
);
CREATE INDEX IF NOT EXISTS customer_guard_extra_delivery_sending
    ON customer_guard_extra_delivery(attempted_at) WHERE delivery='sending';
COMMIT;
