-- поток: mkt
-- 112: флаг «себест протух» в контроле маржи.
--
-- Аудит 07.08.2026: у 116 SKU FIFO-себест из отгрузок МС ниже 70 % живой закупки TheCartridge
-- (медианно на 305 ₽, суммарно 76 тыс ₽). Причина не в ошибке расчёта — товар отгружался давно
-- и с тех пор подорожал, FIFO честно помнит старую цену. Но маржа, посчитанная на такой себест,
-- ЗАВЫШЕНА, а маржа у нас решающий фактор: на неё смотрит и ценообразование, и лестница ставок.
-- Поэтому такие SKU помечаются явно, а не молча остаются в общей массе.
--
-- Флаг ставится, когда есть ОБА числа и FIFO ниже живой закупки более чем на COGS_STALE_GAP
-- (по умолчанию 30 %). Решения по таким SKU принимаются по колонке margin_own_live.

ALTER TABLE mkt_margin_control
    ADD COLUMN IF NOT EXISTS cogs_stale boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN mkt_margin_control.cogs_stale IS
    'FIFO-себест протух: fifo_cogs_u ниже живой закупки более чем на 30 % (товар подорожал с последней отгрузки). Маржа по FIFO завышена — смотреть margin_own_live.';

CREATE INDEX IF NOT EXISTS idx_mmc_stale ON mkt_margin_control (captured_date, cogs_stale)
    WHERE cogs_stale;
