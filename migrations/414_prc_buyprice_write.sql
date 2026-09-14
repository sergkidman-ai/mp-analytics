-- поток: prc — журнал записи закупочной цены карточек из оприходований на «Удаленный».
--
-- Одна строка = одна отправленная правка buyPrice (prices/enter_buyprice.py). Нужна, чтобы
-- задним числом ответить «откуда в карточке эта закупочная» и откатить прогон без МС-аудита:
-- старое значение лежит здесь и в слепке backups/prc_enter_buyprice/<прогон>/before.jsonl.
CREATE TABLE IF NOT EXISTS prc_buyprice_write (
    id           bigserial PRIMARY KEY,
    run_at       timestamptz NOT NULL,
    card_id      uuid        NOT NULL,
    supplier_key text        NOT NULL,
    doc_name     text        NOT NULL,
    old_kop      bigint      NOT NULL,
    new_kop      bigint      NOT NULL
);
CREATE INDEX IF NOT EXISTS prc_buyprice_write_card ON prc_buyprice_write (card_id, run_at);
