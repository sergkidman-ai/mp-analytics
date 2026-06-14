-- migrations/005_wb_cards.sql — карточки WB: nm_id → наш артикул (vendorCode) + название.
CREATE TABLE IF NOT EXISTS wb_cards (
    account TEXT NOT NULL,
    nm_id BIGINT NOT NULL,
    vendor_code TEXT,        -- наш артикул на WB
    title TEXT,              -- название карточки
    brand TEXT,
    subject TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY(account, nm_id)
);
