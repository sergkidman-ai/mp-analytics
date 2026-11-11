-- поток: fin. Manual loss cost fills missing units only, outside MS/financial source data.
CREATE TABLE IF NOT EXISTS wb_loss_manual_cost (
 snapshot_id text NOT NULL REFERENCES wb_loss_recovery_snapshot(id),
 account text NOT NULL CHECK(account IN ('wb_acc1','wb_acc2')),
 warehouse text NOT NULL,
 nm_id bigint NOT NULL,
 unit_cost numeric(12,2) NOT NULL CHECK(unit_cost>0),
 qty integer NOT NULL CHECK(qty>0),
 note text NOT NULL DEFAULT '',
 enabled boolean NOT NULL DEFAULT true,
 version integer NOT NULL DEFAULT 1,
 actor text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(snapshot_id,account,warehouse,nm_id)
);
CREATE TABLE IF NOT EXISTS wb_loss_manual_cost_history (
 id bigserial PRIMARY KEY,
 snapshot_id text NOT NULL,
 account text NOT NULL,
 warehouse text NOT NULL,
 nm_id bigint NOT NULL,
 action text NOT NULL CHECK(action IN ('save','clear')),
 old_value jsonb,
 new_value jsonb NOT NULL,
 actor text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
