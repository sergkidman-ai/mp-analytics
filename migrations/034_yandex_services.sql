-- 034: Сырьё «Отчёта о стоимости услуг маркетплейса» Яндекса (ручная выгрузка из ЛК)
-- и колонки витрины под подписку и баллы за отзыв.
-- Источник: incoming/marketplace_services_financial_month.xlsx (лист на вид услуги).
-- Нужен, потому что API продвижения отдаёт только ~70 дней вглубь — реклама за янв–апр
-- достаётся только из этой выгрузки (буст продаж/показов, Полки, баннеры, отзывы, подписка).

CREATE TABLE IF NOT EXISTS raw_yandex_services (
    account    text        NOT NULL,
    service    text        NOT NULL,   -- имя листа: «Буст продаж, оплата за продажи» и т.п.
    category   text        NOT NULL,   -- свёрнутая категория: ad | subscription | reviews
    ym         text        NOT NULL,   -- 'YYYY-MM' по дате оказания услуги
    svc_date   date,                   -- дата оказания услуги
    order_id   text,
    sku        text,
    cost       numeric     NOT NULL DEFAULT 0,   -- реальная оплата в ₽ (без бонусов)
    bonus      numeric     NOT NULL DEFAULT 0,   -- оплата бонусами (не наши деньги)
    row_hash   text        NOT NULL,   -- дедуп: md5 нормализованной строки
    payload    jsonb       NOT NULL,   -- строка как есть (заголовок→значение)
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, row_hash)
);

CREATE INDEX IF NOT EXISTS idx_yandex_services_ym  ON raw_yandex_services (account, ym);
CREATE INDEX IF NOT EXISTS idx_yandex_services_cat ON raw_yandex_services (account, category, ym);

-- Витрина: подписка (платформенный сбор) и баллы за отзыв — отдельными строками,
-- чтобы не прятать их в other_fee/promotion и видеть на дашборде.
ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS subscription_cost numeric NOT NULL DEFAULT 0;
ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS reviews_cost      numeric NOT NULL DEFAULT 0;
