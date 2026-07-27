-- 200_supplier_payment_terms.sql — поток: inv. График оплаты поставщикам + очередь черновиков
-- платёжек в Альфа-Банк. Три метода: deferred (отсрочка N дней, оплата ПАЧКОЙ неоплаченных
-- заказов в пределах текущего остатка на р/с), prepayment_per_order (предоплата каждого счёта
-- отдельно и сразу), prepayment_balance (аванс наперёд, пополняем при приближении к порогу).
-- Баланс по prepayment_balance считается на лету из payment_draft_queue+МС, отдельного
-- гроссбуха не заводим (принцип 2 ARCHITECTURE.md).

CREATE TABLE IF NOT EXISTS supplier_payment_terms (
    inn               text PRIMARY KEY,
    name              text NOT NULL,
    method            text NOT NULL CHECK (method IN ('deferred', 'prepayment_per_order', 'prepayment_balance')),
    deferral_days     integer,          -- method=deferred
    advance_amount    numeric,          -- method=prepayment_balance: сумма пополнения аванса, ₽
    balance_threshold numeric,          -- method=prepayment_balance: порог для нового аванса, ₽
    active            boolean NOT NULL DEFAULT true,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Один черновик может покрывать несколько заказов (пачка по методу deferred) — поэтому
-- payment_draft_queue создаём раньше, po_payment_status ссылается на неё через draft_id.
CREATE TABLE IF NOT EXISTS payment_draft_queue (
    id               serial PRIMARY KEY,
    inn              text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('deferred_batch', 'prepayment_order', 'advance')),
    amount           numeric NOT NULL,       -- ₽, фактически включённая в черновик сумма
    covers_po_ids    text[],                 -- какие po_payment_status.po_id вошли (NULL для advance)
    status           text NOT NULL DEFAULT 'planned'
                     CHECK (status IN ('planned', 'sent_sandbox', 'sent_prod', 'error')),
    alfa_external_id text,
    note             text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Статус оплаты по каждому заказу поставщику (deferred/prepayment_per_order) — нужен, чтобы
-- видеть весь пул неоплаченного по поставщику и собирать его в пачку при наступлении срока
-- хотя бы по одному заказу.
CREATE TABLE IF NOT EXISTS po_payment_status (
    po_id      text PRIMARY KEY,        -- id purchaseorder в МойСклад
    inn        text NOT NULL,
    order_date date NOT NULL,
    due_date   date NOT NULL,           -- order_date (+ deferral_days для deferred)
    amount     numeric NOT NULL,        -- ₽
    status     text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'queued', 'paid')),
    draft_id   integer REFERENCES payment_draft_queue(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_po_payment_status_inn_status ON po_payment_status (inn, status);
CREATE INDEX IF NOT EXISTS idx_payment_draft_queue_status ON payment_draft_queue (status);
