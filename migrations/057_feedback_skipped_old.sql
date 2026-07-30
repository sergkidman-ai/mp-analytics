-- поток: rev
-- Вопросы старше 30 дней без ответа: не генерируем черновик, не шлём в модерацию,
-- помечаем флагом вместо повторной обработки каждый цикл.
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS skipped_old boolean NOT NULL DEFAULT false;
