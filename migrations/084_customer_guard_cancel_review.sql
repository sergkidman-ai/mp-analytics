-- поток: fin. Local classification of canceled marketplace orders; no MS writes.
CREATE TABLE IF NOT EXISTS customer_guard_cancel_review (
  order_id bigint PRIMARY KEY REFERENCES customer_guard_orders(id),
  category text NOT NULL CHECK (category IN (
    'Отменён — товар не покидал наш склад',
    'Отменён — путь товара выясняется',
    'Отменён — передан МП, маршрут возврата выясняется',
    'Невыкуп → наш склад', 'Невыкуп → склад МП',
    'Возврат после вручения → наш склад', 'Возврат после вручения → склад МП',
    'Возврат принят нами', 'Возврат принят складом МП')),
  first_cancelled_at timestamptz NOT NULL,
  first_handoff_at timestamptz,
  buyer_received_at timestamptz,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  reviewed_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_guard_cancel_review_recent
  ON customer_guard_cancel_review(first_cancelled_at DESC);
CREATE TABLE IF NOT EXISTS customer_guard_cancel_review_history (
  id bigserial PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
  category text NOT NULL,
  observed_at timestamptz NOT NULL,
  evidence jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_guard_cancel_review_history_order
  ON customer_guard_cancel_review_history(order_id,id DESC);
