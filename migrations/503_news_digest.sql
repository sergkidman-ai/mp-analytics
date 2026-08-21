-- поток: ev
-- Выжимка по денежным новостям площадок: что меняется, на сколько, с какого числа,
-- как бьёт по нашей марже и что сделать. Живёт рядом с сырьём, а не вместо него:
-- сырьё неприкосновенно (правило 2), выжимку можно пересчитать другой моделью.
ALTER TABLE mp_notices ADD COLUMN IF NOT EXISTS digest    jsonb;
ALTER TABLE mp_notices ADD COLUMN IF NOT EXISTS digest_at timestamptz;
