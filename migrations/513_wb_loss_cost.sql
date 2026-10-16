-- поток: ev. Actual MS shipment cost snapshots; no writes to financial marts.
CREATE TABLE IF NOT EXISTS wb_loss_demand_cost (
 account text NOT NULL, demand_id text NOT NULL, payload jsonb NOT NULL,
 total numeric, state text NOT NULL, built_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account,demand_id)
);
CREATE TABLE IF NOT EXISTS wb_loss_cost_result (
 account text PRIMARY KEY, payload jsonb NOT NULL, built_at timestamptz NOT NULL DEFAULT now()
);
