-- поток: rev
-- CardFacts.for_wb() читает карточку точечно по nmID (раньше тянул таблицу целиком в память и после
-- подключения wb_acc2 ронял прогон по OOM). В PK (account, nm_id) поиск только по nm_id идёт вторым
-- ключом, т.е. seq-scan по 29k строк на каждый вызов — добавляем прямой индекс.
CREATE INDEX IF NOT EXISTS raw_wb_card_content_nm_idx ON raw_wb_card_content (nm_id);
