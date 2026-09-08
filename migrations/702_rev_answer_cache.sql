-- поток: rev — блок G брифа 08.09.2026: кэш утверждённых человеком ответов по НАШЕМУ артикулу.
-- Причина: 01.09.2026 вопрос «каким тонером их заправлять?» пришёл по одному артикулу 5422 на
-- oz_acc1, oz_acc2 и wb_acc2 и получил три РАЗНЫХ ответа — каждый черновик собирался заново.
-- Ключ намеренно не по SKU площадки: один товар живёт под четырьмя разными кодами площадок.
--
-- card_sig / stale — не из списка полей брифа, а обязательное условие пункта «инвалидация»:
-- без отпечатка карточки на момент утверждения нельзя узнать, что совместимость или атрибут
-- ключа с тех пор изменились, и кэш начнёт отвечать вчерашней правдой.
CREATE TABLE IF NOT EXISTS approved_answers (
    id              bigserial PRIMARY KEY,
    article         text        NOT NULL,          -- наш внутренний артикул (external_code МС)
    request_class   text        NOT NULL,          -- reports/request_class.py
    question_key    text        NOT NULL,          -- reports/answer_cache.question_key()
    answer_text     text        NOT NULL,
    approved_by     text        NOT NULL,          -- 'send' | 'edit' | 'backfill'
    approved_at     timestamptz NOT NULL DEFAULT now(),
    source_platform text,
    source_account  text,
    source_ext_id   text,
    card_sig        text,                          -- отпечаток полей карточки, из которых собран ключ
    stale           boolean     NOT NULL DEFAULT false,
    stale_reason    text,
    stale_at        timestamptz,
    hit_count       integer     NOT NULL DEFAULT 0,
    last_hit_at     timestamptz,
    CONSTRAINT approved_answers_key UNIQUE (article, request_class, question_key)
);
CREATE INDEX IF NOT EXISTS approved_answers_article_idx ON approved_answers (article);
