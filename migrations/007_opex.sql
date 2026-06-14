-- migrations/007_opex.sql — постоянные расходы бизнеса (ФОТ + аренда), не привязаны к WB-отчёту.
-- Действуют с effective_from; amount = base*(1+tax_pct). Это бизнес-уровень (оба юрлица вместе).
CREATE TABLE IF NOT EXISTS opex (
    id SERIAL PRIMARY KEY,
    effective_from DATE NOT NULL,
    category TEXT NOT NULL,        -- salary | rent
    name TEXT NOT NULL,
    role TEXT,
    base NUMERIC NOT NULL,
    tax_pct NUMERIC NOT NULL DEFAULT 0,
    amount NUMERIC NOT NULL,       -- base*(1+tax_pct)
    UNIQUE(effective_from, name)
);
