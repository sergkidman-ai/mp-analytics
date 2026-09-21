-- поток: rev
-- п.8 пакета 21.09.2026: кэш утверждённых ответов — запасной поиск по семейству кода расходника.
-- Кейс: «963XL на OfficeJet 9010» спрашивали на 4147 и на 41462 — артикулы разные, код один;
-- второй раз кэш не сработал, ответ собирался заново. Код расходника — card_facts['code'].
ALTER TABLE approved_answers ADD COLUMN IF NOT EXISTS code text;
CREATE INDEX IF NOT EXISTS approved_answers_code_idx
    ON approved_answers (code, request_class, question_key) WHERE code IS NOT NULL;
