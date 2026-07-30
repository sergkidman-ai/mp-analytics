-- поток: rev
-- Кэш черновика по id+тексту: без этого feedback_today.run() перегенерировал ответ (в т.ч. вызов
-- Opus на ВОПРОСЫ) для ВСЕГО неотвеченного окна на КАЖДОМ цикле — 3 цикла за день дали 3x одинаковых
-- 15 ИИ-вызовов на те же 16 вопросов. Хэш содержимого (body+pros+cons) фиксируется при драфте;
-- при неизменном содержимом цикл черновик не трогает.
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS draft_src_hash text;

-- backfill: у уже задрафченных строк хэш пуст — без этого первый же цикл после миграции ещё раз
-- перегенерил бы их все (искали пустой хэш как «нужен дозадрафт»). Bulk SQL, без вызова LLM.
UPDATE raw_feedback SET draft_src_hash = md5(coalesce(body,'')||coalesce(pros,'')||coalesce(cons,''))
WHERE draft_text IS NOT NULL AND draft_src_hash IS NULL;
