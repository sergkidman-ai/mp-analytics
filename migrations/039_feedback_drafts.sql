-- 039: поля пайплайна черновиков ответов. Режим «только черновики»: генерим draft_text,
-- маршрут (auto — безопасно к авто-постингу позже; review — на человека) и уверенность;
-- grounding — какие факты карточки использованы (для аудита проверки данных). Постинг —
-- отдельный шаг, фиксируется в posted_at/posted_ok.
ALTER TABLE raw_feedback
    ADD COLUMN IF NOT EXISTS draft_text       text,
    ADD COLUMN IF NOT EXISTS draft_route      text,      -- auto | review
    ADD COLUMN IF NOT EXISTS draft_confidence numeric,   -- 0..1
    ADD COLUMN IF NOT EXISTS draft_category   text,      -- empty5 | positive | negative | question
    ADD COLUMN IF NOT EXISTS draft_grounding  jsonb,
    ADD COLUMN IF NOT EXISTS draft_at         timestamptz,
    ADD COLUMN IF NOT EXISTS posted_at        timestamptz,
    ADD COLUMN IF NOT EXISTS posted_ok        boolean;
