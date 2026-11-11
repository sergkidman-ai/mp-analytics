-- поток: fin. Existing WB normalizers and dashboard read this separate amount.
-- NULL preserves an unknown amount; historical data must come from the report.
ALTER TABLE sales ADD COLUMN IF NOT EXISTS revenue_wb numeric;
