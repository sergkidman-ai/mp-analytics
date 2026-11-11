-- поток: fin. ISO API dates permit indexed half-open month ranges.
CREATE INDEX IF NOT EXISTS bi_wb_report_created_idx
    ON raw_wb_report (account, (payload->>'create_dt'));
CREATE INDEX IF NOT EXISTS bi_ozon_transaction_date_idx
    ON raw_ozon_transaction (account, (payload->>'operation_date'));
CREATE INDEX IF NOT EXISTS bi_wb_report_assembly_idx
    ON raw_wb_report (account, (payload->>'assembly_id'));
