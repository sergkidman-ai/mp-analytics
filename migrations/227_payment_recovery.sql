-- поток: inv. Preserve provenance for recovered MS payments and hold rebuilt drafts.
-- Existing rows retain the legacy externalId derived from their integer id.
ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS bank_external_seed uuid;
ALTER TABLE payment_draft_queue ALTER COLUMN bank_external_seed SET DEFAULT gen_random_uuid();
ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS recovery_hold boolean NOT NULL DEFAULT false;

ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_draft_queue_kind_check;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_draft_queue_kind_check CHECK
    (kind IN ('deferred_batch','prepayment_order','advance','rent','rent_utility','hosting','ms_payment'));
ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_recovery_history_paid;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_recovery_history_paid CHECK
    (kind <> 'ms_payment' OR (status = 'paid' AND ms_payment_id IS NOT NULL));
CREATE UNIQUE INDEX IF NOT EXISTS payment_recovery_ms_identity
    ON payment_draft_queue(org_inn, ms_payment_id) WHERE kind = 'ms_payment';
