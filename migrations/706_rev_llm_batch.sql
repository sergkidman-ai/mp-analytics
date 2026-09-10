-- поток: rev — отложенная очередь вызовов модели (Message Batches API, −50 % к цене).
--
-- Зачем таблицы, а не очередь в памяти: цикл отзывов живёт 2 часа и умирает вместе с процессом,
-- а батч отвечает до часа. Между «запрос собран» и «ответ пришёл» обязан пережить перезапуск,
-- иначе половина черновиков теряется молча — ровно тот класс дыры, что дал 1147 пустых вызовов
-- по отзывам Ozon (A3, 07.09.2026).

-- Собранные, но ещё не отправленные (batch_id IS NULL) и отправленные запросы.
CREATE TABLE IF NOT EXISTS feedback_llm_queue (
    id           bigserial PRIMARY KEY,
    req_key      text NOT NULL UNIQUE,     -- md5 промпта+записи: тот же вопрос не уедет дважды
    platform     text,
    account      text,
    kind         text,
    ext_id       text,
    purpose      text NOT NULL DEFAULT 'draft',   -- draft | verify
    model        text NOT NULL,
    max_tokens   integer NOT NULL DEFAULT 3000,
    system_text  text NOT NULL,
    content      text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    batch_id     text,
    submitted_at timestamptz
);
CREATE INDEX IF NOT EXISTS feedback_llm_queue_pending
    ON feedback_llm_queue (created_at) WHERE batch_id IS NULL;

-- Пришедшие ответы. Живут и после использования: повторный прогон той же записи (а он бывает —
-- запись без черновика берётся следующим циклом снова) обязан взять готовый ответ, а не платить ещё раз.
CREATE TABLE IF NOT EXISTS feedback_llm_result (
    req_key    text PRIMARY KEY,
    batch_id   text,
    model      text,
    raw        text,
    tokens_in  integer DEFAULT 0,
    tokens_out integer DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at    timestamptz
);

CREATE TABLE IF NOT EXISTS feedback_llm_batch (
    batch_id   text PRIMARY KEY,
    model      text,
    n_req      integer NOT NULL DEFAULT 0,
    status     text NOT NULL DEFAULT 'submitted',   -- submitted | ended | failed
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    n_ok       integer NOT NULL DEFAULT 0,
    n_err      integer NOT NULL DEFAULT 0
);
