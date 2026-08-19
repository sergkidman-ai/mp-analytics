-- поток: fin
-- Достройка к 068: зачёт баллами по КАЖДОЙ строке продвижения отдельно.
-- Нужно, чтобы под-строки «Буст-продажи / Буст-показы / Полки» были показаны начисленными
-- так же, как родительская «Продвижение», и сумма частей сходилась с родителем.
-- (У «Программы лояльности» и рекламы по договору зачёта не бывает — проверено янв–авг 2026.)
ALTER TABLE yandex_finance_monthly
    ADD COLUMN IF NOT EXISTS netting_boost_sales numeric(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS netting_boost_shows numeric(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS netting_shelf       numeric(14,2) DEFAULT 0;
