-- поток: mkt
-- Управление ставками WB: журнал смен + ручные/точные текущие ставки + замер последствий.
--
-- Контекст (память project_mp_wb_ads_endpoint): ставка WB — CPC ПО КАЖДОМУ nmId (не на кампанию),
-- шаг 1 коп. Геттера ТЕКУЩЕЙ назначенной ставки у WB НЕТ → «текущую» реконструируем как
-- факт.CPC = расход/клики из wb_ad_nm (только по кликнутым SKU). Где кликов не было — ставку
-- проставляем вручную (wb_bid_override), чтобы картина была целой. Каждую смену пишем в журнал
-- (wb_bid_log) и меряем влияние на трафик/позицию/заказы по Джему (wb_search_report, ежедневный).

-- ── Ручные / точные (api) текущие ставки per-nmID (оверрайд над реконструкцией) ────────────────
CREATE TABLE IF NOT EXISTS wb_bid_override (
  account     text        NOT NULL,
  nm_id       bigint      NOT NULL,
  cpc         numeric     NOT NULL,                    -- ставка ₽ (наша известная текущая)
  source      text        NOT NULL DEFAULT 'manual',   -- manual | api_set
  advert_id   bigint,                                  -- в какой кампании наблюдали/ставили (инфо)
  note        text,
  author      text        DEFAULT 'dashboard',
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account, nm_id)
);
COMMENT ON TABLE wb_bid_override IS
  'Ручные/точные (api_set) текущие ставки per-nmID — оверрайд над реконструкцией CPC=расход/клики. '
  'Заполняет пробелы, где кликов не было, чтобы картина ставок была целой. Поток mkt.';

-- ── Журнал смен ставок + baseline последствий ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wb_bid_log (
  id          bigserial   PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  account     text        NOT NULL,
  nm_id       bigint      NOT NULL,
  advert_id   bigint,
  action      text        NOT NULL,             -- manual_set | api_set | api_set_min | dry_run
  applied     boolean     NOT NULL DEFAULT false, -- true = ушло в WB живьём; false = dry-run / только запись
  old_cpc     numeric,
  new_cpc     numeric,
  old_source  text,                             -- reconstructed | manual | api_set | none
  author      text        DEFAULT 'dashboard',
  note        text,
  req_json    jsonb,                            -- что отправили бы/отправили в WB (аудит)
  resp_status int,
  resp_json   jsonb,
  -- baseline последствий на момент смены (снимок Джема); «после» считаем на чтении из свежего Джема
  base_date         date,                       -- дата ближайшего Джем-среза на момент смены
  pos_before        numeric,                    -- avg_position Джема на момент смены
  open_before       int,                        -- показы карточки (open_card) на момент смены
  orders_before     int                         -- заказы Джема (7-дн окно) на момент смены
);
CREATE INDEX IF NOT EXISTS ix_wb_bid_log_nm  ON wb_bid_log (account, nm_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_wb_bid_log_ts  ON wb_bid_log (ts DESC);
COMMENT ON TABLE wb_bid_log IS
  'Аудит смен ставок WB (ручных/dry-run/api) + baseline последствий по Джему. Дельту «до/после» '
  '(позиция/показы/заказы) считает эндпоинт из свежего wb_search_report. Поток mkt.';
