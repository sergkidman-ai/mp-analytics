-- поток: ev
-- Сырьё новостей площадок: сообщения официальных каналов как есть.
-- Отдельно от biz_events сознательно (правило 2 проекта): в дневник попадает только важное,
-- а переклассифицировать «важное» без повторного похода в API можно лишь по сырью.
CREATE TABLE IF NOT EXISTS mp_notices (
    platform    text        NOT NULL,               -- ozon | wb | yandex
    account     text        NOT NULL,
    message_id  text        NOT NULL,
    chat_id     text,
    chat_type   text,
    created_at  timestamptz NOT NULL,
    title       text,
    body        text,
    importance  text        NOT NULL DEFAULT 'info'
                            CHECK (importance IN ('alert', 'watch', 'info')),
    matched     text,                               -- какое правило сработало
    event_id    bigint,                             -- запись в biz_events, если завели
    captured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account, message_id)
);
CREATE INDEX IF NOT EXISTS mp_notices_date_idx ON mp_notices (created_at DESC);
CREATE INDEX IF NOT EXISTS mp_notices_alert_idx ON mp_notices (importance, created_at DESC)
    WHERE importance <> 'info';
