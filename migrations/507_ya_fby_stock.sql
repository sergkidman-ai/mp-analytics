-- поток: ev — остатки на складах Яндекс.Маркета (FBY) для еженедельной задачи вывоза.
-- Повод: с 01.09.2026 Маркет вводит фиксированный льготный срок хранения (120 дней для наших
-- категорий), после него хранение платное — залежавшийся товар нужно вывозить, как с Ozon FBO.
CREATE TABLE IF NOT EXISTS ya_fby_stock (
    account      text    NOT NULL,
    campaign_id  bigint  NOT NULL,
    warehouse_id bigint  NOT NULL,
    warehouse    text,
    offer_id     text    NOT NULL,
    available    integer NOT NULL DEFAULT 0,   -- доступно к продаже
    frozen       integer NOT NULL DEFAULT 0,   -- заморожено под заказы
    defect       integer NOT NULL DEFAULT 0,
    expired      integer NOT NULL DEFAULT 0,
    turnover_days integer,                     -- оборачиваемость от Маркета, дней
    turnover      text,                        -- градация Маркета (LOW/NORMAL/HIGH)
    updated_at   timestamp,                    -- когда Маркет обновил остаток
    captured_at  date    NOT NULL,
    PRIMARY KEY (account, campaign_id, warehouse_id, offer_id, captured_at));
CREATE INDEX IF NOT EXISTS ya_fby_stock_day ON ya_fby_stock (captured_at, account);
