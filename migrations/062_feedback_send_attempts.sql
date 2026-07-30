-- поток: rev
-- Счётчик неудачных попыток отправки ответа на площадку.
-- Зачем: раньше провал отправки молча помечал raw_feedback.posted_at (posted_ok=false) и ответ
-- исчезал из всех очередей — «отправлено» его больше не считало, но покупатель ответа не увидел.
-- Теперь провал НЕ закрывает строку: она остаётся к повтору, пока попыток < FEEDBACK_SEND_MAX_ATTEMPTS
-- (по умолчанию 3). На третьей — blocked=true, повторы прекращаются, в Telegram уходит ⛔-сообщение,
-- и в суточной сводке такие случаи идут ОТДЕЛЬНОЙ строкой, а не в общем счётчике ошибок.
CREATE TABLE IF NOT EXISTS feedback_send_attempts (
    platform    text        NOT NULL,
    account     text        NOT NULL,
    kind        text        NOT NULL,
    ext_id      text        NOT NULL,
    attempts    int         NOT NULL DEFAULT 0,
    last_error  text,
    first_at    timestamptz NOT NULL DEFAULT now(),
    last_at     timestamptz NOT NULL DEFAULT now(),
    blocked     boolean     NOT NULL DEFAULT false,   -- лимит исчерпан: повторы прекращены
    alerted_at  timestamptz,                          -- когда ⛔ ушло в Telegram (не дублируем)
    PRIMARY KEY (platform, account, kind, ext_id)
);

CREATE INDEX IF NOT EXISTS feedback_send_attempts_blocked_idx
    ON feedback_send_attempts (blocked, last_at DESC) WHERE blocked;
