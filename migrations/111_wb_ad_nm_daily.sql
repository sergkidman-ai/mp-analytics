-- поток: mkt
-- Дневной трек рекламы per-nmID: показы/клики/заказы/расход + позиция рекламного бустера.
-- Источник — /adv/v3/fullstats: days[].apps[].nms[] (посуточно) и boosterStats[] {nm,date,avg_position}.
-- Нужен, чтобы видеть РЕАКЦИЮ на смену ставки каждый день (Джем — органика с лагом 7д, не показатель).
CREATE TABLE IF NOT EXISTS wb_ad_nm_daily (
  account     text        NOT NULL,
  dt          date        NOT NULL,
  advert_id   bigint      NOT NULL,
  nm_id       bigint      NOT NULL,
  views       integer     DEFAULT 0,          -- рекламные показы карточки за день
  clicks      integer     DEFAULT 0,
  atbs        integer     DEFAULT 0,           -- добавления в корзину
  orders      integer     DEFAULT 0,
  spend       numeric(12,2) DEFAULT 0,
  revenue     numeric(12,2) DEFAULT 0,
  cpc         numeric(10,2),                    -- факт. ставка дня = расход/клики
  booster_pos numeric(7,2),                     -- средняя позиция рекламного бустера за день
  updated_at  timestamptz DEFAULT now(),
  PRIMARY KEY (account, dt, advert_id, nm_id)
);
CREATE INDEX IF NOT EXISTS ix_wb_ad_nm_daily_nm ON wb_ad_nm_daily (account, nm_id, dt DESC);
