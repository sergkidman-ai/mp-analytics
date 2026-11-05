-- поток: rev — saved manual text must remain confirmable after a Telegram edit failure
ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_edit_card_ok boolean NOT NULL DEFAULT false;
