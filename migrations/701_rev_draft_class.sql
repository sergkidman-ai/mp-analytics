-- поток: rev — A2 брифа 07.09.2026: чем именно ответил бот и на что.
-- До правки в raw_feedback лежал только готовый текст черновика: какой шаблон подставлен —
-- восстанавливалось лишь пересчётом _pick(ext_id) и терялось при любой правке списка вариантов,
-- а класс обращения у отзывов не писался вовсе (нечем мерить блок H и не на чем строить блок I).
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS template_id   text;
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS request_class text;
CREATE INDEX IF NOT EXISTS raw_feedback_request_class_idx ON raw_feedback (request_class);
