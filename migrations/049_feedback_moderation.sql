-- 040: очередь модерации ответов на ВОПРОСЫ покупателей (боевой режим с ручным подтверждением
-- в Telegram). Движок кладёт сюда предложенный ответ (текст живёт в raw_feedback.draft_text),
-- бот-модератор шлёт карточку в ТГ и по кнопке «Отправить»/«Править»/«Пропустить» переводит
-- состояние. Реальный постинг фиксируется в raw_feedback.posted_at/posted_ok (миграция 039).
-- kind пока всегда 'question' (отзывы вне охвата боевого режима на старте).
CREATE TABLE IF NOT EXISTS feedback_moderation (
    id           bigserial PRIMARY KEY,
    platform     text NOT NULL,
    account      text NOT NULL,
    kind         text NOT NULL,                       -- 'question'
    ext_id       text NOT NULL,
    tg_chat_id   bigint,
    tg_msg_id    bigint,
    state        text NOT NULL DEFAULT 'queued',      -- queued|carded|sent|skipped|failed
    final_text   text,                                -- одобренный/исправленный текст, реально ушедший
    error        text,
    enqueued_at  timestamptz NOT NULL DEFAULT now(),
    carded_at    timestamptz,
    decided_at   timestamptz,
    decided_by   bigint,                              -- telegram user id, принявший решение
    UNIQUE (platform, account, kind, ext_id)
);

CREATE INDEX IF NOT EXISTS ix_feedback_moderation_state ON feedback_moderation (state);
