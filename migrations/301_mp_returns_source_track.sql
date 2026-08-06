-- поток: ret — два новых источника возвратов Ozon (06.08.2026).
--   ozon_rfbs    — Real-FBS (экспресс): возврат едет Почтой России, забирают по треку;
--   ozon_removal — вывоз со склада FBO: коробка приезжает в пункт выдачи.
-- Оба пишутся в platform = 'ozon', поэтому нужен `source`: пометка «пропал из выдачи API»
-- (`gone_at`) делается в пределах одного источника, иначе один источник гасил бы чужие строки.

ALTER TABLE mp_returns ADD COLUMN IF NOT EXISTS source       text;
ALTER TABLE mp_returns ADD COLUMN IF NOT EXISTS track_number text;   -- трек Почты России (rFBS)

-- всё, что собрано до этой миграции, — классический /v1/returns/list и площадки целиком
UPDATE mp_returns SET source = platform WHERE source IS NULL;

COMMENT ON COLUMN mp_returns.source IS
    'ozon | ozon_rfbs | ozon_removal | yandex | wb — сборщик, из которого пришла строка';
COMMENT ON COLUMN mp_returns.track_number IS
    'трек-номер Почты России: по нему получают возврат Real-FBS (ru_post_tracking_number)';

CREATE INDEX IF NOT EXISTS idx_mp_returns_source
    ON mp_returns (source, account) WHERE gone_at IS NULL;
