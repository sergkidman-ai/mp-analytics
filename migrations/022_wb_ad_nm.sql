-- Реклама WB на уровне товара (nmId) внутри кампании.
-- Источник: /adv/v3/fullstats → days[].apps[].nms[] (cpc = факт. ставка/клик по nmId в кампании).
-- Связка «методов»: promotion/count даёт id+тип кампании, fullstats nms[] — ставку/расход по nmId.
CREATE TABLE IF NOT EXISTS wb_ad_nm (
    account     TEXT NOT NULL,
    period      DATE NOT NULL,
    advert_id   BIGINT NOT NULL,
    nm_id       BIGINT NOT NULL,
    adv_type    INT,
    status      INT,
    name        TEXT,
    clicks      INT,
    views       INT,
    atbs        INT,        -- добавления в корзину
    orders      INT,
    spend       NUMERIC,
    revenue     NUMERIC,
    cpc         NUMERIC,    -- spend/clicks (факт. ставка за клик по товару в кампании)
    ctr         NUMERIC,
    cr          NUMERIC,    -- клик→заказ
    drr         NUMERIC,    -- spend/revenue*100
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period, advert_id, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_wb_ad_nm_nm ON wb_ad_nm (account, nm_id);
