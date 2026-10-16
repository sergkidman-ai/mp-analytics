-- поток: prc — исходные факты WB-названия отдельно от назначенного внешнего кода.
-- Подготовленная запись остаётся при неясном ответе МС: повторный POST автоматически не делаем.
CREATE TABLE IF NOT EXISTS prc_wb_name_source (
    card_code text PRIMARY KEY,
    ms_id uuid UNIQUE,
    external_code text NOT NULL,
    source jsonb NOT NULL,
    source_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prc_wb_name_source_external ON prc_wb_name_source (external_code);
