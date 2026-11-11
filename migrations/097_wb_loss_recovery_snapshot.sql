-- поток: fin. Recovered loss evidence is separate from official PDFs and live inventory.
CREATE TABLE IF NOT EXISTS wb_loss_recovery_snapshot (
 id text PRIMARY KEY,
 payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO wb_damage_stock_warehouse(warehouse) VALUES ('Владимир'),('СЦ Шушары') ON CONFLICT DO NOTHING;
