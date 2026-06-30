-- WB Джем (Аналитика поисковых запросов): позиции в выдаче + запросы по товару.
-- Источник: seller-analytics-api.wildberries.ru/api/v2/search-report/*  (нужна подписка «Джем»).

-- Сводка по аккаунту/периоду (commonInfo + positionInfo + visibility).
CREATE TABLE IF NOT EXISTS wb_search_summary (
    account         TEXT NOT NULL,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    supplier_rating NUMERIC,
    advertised      INT,
    total_products  INT,
    avg_position    INT,  avg_position_dyn    INT,
    median_position INT,  median_position_dyn INT,
    visibility      INT,  visibility_dyn      INT,
    open_card       INT,  open_card_dyn       INT,
    first_hundred   INT,  first_hundred_dyn   INT,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start)
);

-- Позиции/воронка по каждому товару (groups[].items[]). dynamics = % к прошлому периоду.
CREATE TABLE IF NOT EXISTS wb_search_report (
    account        TEXT NOT NULL,
    period_start   DATE NOT NULL,
    nm_id          BIGINT NOT NULL,
    name           TEXT,
    vendor_code    TEXT,
    subject_name   TEXT,
    brand          TEXT,
    is_advertised  BOOLEAN,
    rating         NUMERIC,
    feedback_rating NUMERIC,
    min_price      INT,
    max_price      INT,
    avg_position   INT,  avg_position_dyn   INT,
    open_card      INT,  open_card_dyn      INT,
    add_to_cart    INT,  add_to_cart_dyn    INT,
    open_to_cart   INT,  open_to_cart_dyn   INT,
    orders         INT,  orders_dyn         INT,
    cart_to_order  INT,  cart_to_order_dyn  INT,
    visibility     INT,  visibility_dyn     INT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start, nm_id)
);

-- Поисковые запросы по товару (product/search-texts items[]).
CREATE TABLE IF NOT EXISTS wb_search_text (
    account        TEXT NOT NULL,
    period_start   DATE NOT NULL,
    nm_id          BIGINT NOT NULL,
    text           TEXT NOT NULL,
    frequency      INT,  frequency_dyn      INT,
    week_frequency INT,
    median_position INT, median_position_dyn INT,
    avg_position   INT,  avg_position_dyn   INT,
    open_card      INT,  open_card_dyn      INT,  open_card_pct  INT,
    add_to_cart    INT,  add_to_cart_dyn    INT,
    open_to_cart   INT,  open_to_cart_dyn   INT,
    orders         INT,  orders_dyn         INT,
    cart_to_order  INT,
    visibility     INT,  visibility_dyn     INT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start, nm_id, text)
);

CREATE INDEX IF NOT EXISTS idx_wb_search_report_orders_dyn ON wb_search_report (account, orders_dyn);
CREATE INDEX IF NOT EXISTS idx_wb_search_text_nm ON wb_search_text (account, nm_id);
