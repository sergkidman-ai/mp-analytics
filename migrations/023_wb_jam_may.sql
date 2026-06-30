-- Джем: текущая неделя vs майская база (до роста цен) — для глобальной сегментации просадки.
CREATE TABLE IF NOT EXISTS wb_jam_may (
    account      TEXT NOT NULL,
    nm_id        BIGINT NOT NULL,
    name         TEXT,
    is_advertised BOOLEAN,
    price        INT,
    pos          INT,  pos_dyn      INT,   -- позиция и изменение пунктов к маю (+ = упали ниже)
    orders       INT,  orders_dyn   INT,
    open         INT,  open_dyn     INT,
    visibility   INT,  vis_dyn      INT,
    updated_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);
