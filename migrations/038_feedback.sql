-- 038: сырьё отзывов и вопросов покупателей (для ответов). Пока WB Цифровой (acc1) +
-- Ozon Премиум (oz_acc1) — там, где API-доступ полный. Одна таблица на обе сущности
-- (kind=review|question) и обе площадки; сырой ответ API целиком в payload.
CREATE TABLE IF NOT EXISTS raw_feedback (
    platform     text NOT NULL,            -- wb | ozon
    account      text NOT NULL,
    kind         text NOT NULL,            -- review | question
    ext_id       text NOT NULL,            -- id отзыва/вопроса у площадки
    item_id      text,                     -- nmId (wb) | sku (ozon)
    article      text,                     -- supplierArticle (wb)
    product_name text,
    rating       int,                      -- только review (1..5)
    body         text,                     -- основной текст
    pros         text,                     -- WB: достоинства
    cons         text,                     -- WB: недостатки
    created_at   timestamptz,
    is_answered  boolean NOT NULL DEFAULT false,
    answer_text  text,
    status       text,                     -- сырой статус площадки
    payload      jsonb NOT NULL,
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account, kind, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_feedback_unans
    ON raw_feedback (platform, account, kind) WHERE is_answered = false;
CREATE INDEX IF NOT EXISTS idx_raw_feedback_item ON raw_feedback (item_id);
