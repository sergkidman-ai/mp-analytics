-- 044: КЭШ СОВМЕСТИМОСТИ. Главный рычаг экономии на веб-поиске (источник №3 движка вопросов).
-- Покупатели спрашивают про одни и те же популярные принтеры снова и снова; веб-проверка
-- «подойдёт ли наш картридж к принтеру X» стоит ~$0.05–0.75 за вызов. Кэшируем результат по паре
-- (наш товар × нормализованная модель принтера) → веб по конкретной паре платится ОДИН раз за всю
-- историю, дальше берётся из БД бесплатно и мгновенно. Не зависит от провайдера LLM.

CREATE TABLE IF NOT EXISTS compat_cache (
    platform    text NOT NULL,             -- wb | ozon
    item_id     text NOT NULL,             -- наш товар (nm_id / sku) как строка
    model_norm  text NOT NULL,             -- нормализованная модель принтера покупателя (ключ)
    model_raw   text,                      -- как её написал покупатель (для отладки)
    verdict     text NOT NULL,             -- yes | no | unclear
    reply       text,                      -- готовый текст ответа про совместимость этой пары
    source      text,                      -- веб | модель | карточка-серия (чем получен вердикт)
    sources     jsonb,                     -- ссылки-источники веб-поиска (если были)
    note        text,
    hits        int NOT NULL DEFAULT 0,    -- сколько раз отдан из кэша (популярность модели)
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, item_id, model_norm)
);
CREATE INDEX IF NOT EXISTS idx_compat_cache_model ON compat_cache (model_norm);
