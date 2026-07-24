-- 041: кнопка «Позже» в модерации — отложить карточку до snooze_until, затем переслать заново.
-- Строка уходит в state='snoozed'; когда snooze_until<=now(), бот присылает новую карточку и
-- переводит её обратно в 'carded'. «Пропустить» остаётся окончательным (state='skipped').
ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS snooze_until timestamptz;
