-- 031: каталог офферов Яндекс.Маркета (offer-mappings) — сырьё для связки ЯМ↔МС.
-- В оффере: offerId (=shopSku), barcodes[], vendorCode, purchasePrice (закупочная из карточки),
-- mapping.marketSku. Связка себеста: offerId→yandex_cost | external_code | barcodes→ms_barcode→МС |
-- purchasePrice; остальное — импутация.

CREATE TABLE IF NOT EXISTS raw_yandex_offer (
    account   text        NOT NULL,
    offer_id  text        NOT NULL,
    payload   jsonb       NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, offer_id)
);
