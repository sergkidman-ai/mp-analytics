-- поток: rev — durable review of recovered cards and expert-source provenance
ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_revision text;
ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_sent_card_ok boolean NOT NULL DEFAULT false;
ALTER TABLE feedback_llm_queue ADD COLUMN IF NOT EXISTS submission_key text;
CREATE TABLE IF NOT EXISTS rev_card_reprocess(
    mod_id bigint PRIMARY KEY, state text NOT NULL DEFAULT 'pending',
    error_type text, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rev_expert_evidence(
    evidence_key text PRIMARY KEY, brand_note_id int, source_path text NOT NULL,
    source_sha256 text NOT NULL, anchor text NOT NULL, source_date date,
    approval_basis text NOT NULL, applicability jsonb NOT NULL,
    status text NOT NULL, restored_at timestamptz DEFAULT now()
);
