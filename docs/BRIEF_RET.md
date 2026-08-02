# BRIEF_RET — возвраты товара (что физически забрать с ПВЗ)

> `ret` ≠ `rev`. Здесь **физика возврата** (коробка лежит в ПВЗ, её надо забрать).
> Отзывы и вопросы покупателей — поток `rev`. Деньги возвратов и сторно COGS — поток `fin`.

**Ветка / worktree:** `ret/returns-bot` в `.claude/worktrees/ret` (в `main` НЕ влито).
**Модель сессии:** Sonnet (механика: API площадок, сбор, рассылка).

## Задача

Отдельный Telegram-бот раз в день присылает полный список висящих FBS-возвратов по
Ozon / Яндекс / WB: сгруппировано по ПВЗ, с адресом, составом возврата, сроком забрать
и штрихкодом получения (картинкой — там, где площадка его отдаёт).

## Сделано

**Фаза 1 — разведка (02.08.2026).** Живые probe-запросы, отчёт `docs/reports/returns_api_recon.md`.
- Ozon `POST /v1/returns/list` — 200 по обоим аккаунтам, пагинация `last_id`/`has_next`, limit 500.
  Машинный статус — `visual.status.sys_name` (русский `display_name` только для показа).
  Деньги приходят объектом `{"currency_code":"RUB","price":N}`. `place` = где возврат сейчас,
  `target_place` = куда приедет (туда и ехать). `logistic.final_moment` непустой = процесс закрыт.
- Ozon `POST /v1/return/giveout/barcode` + `/get-png` — 200, отдаёт **готовый PNG** штрихкода
  получения возвратов (один на аккаунт). `giveout/list` требует `limit` в (0,1000].
- Яндекс `GET /campaigns/{cid}/returns` — 200 по всем 3 кампаниям, пагинация `page_token`.
  `pickupTillDate` заполняется ровно при `shipmentStatus == READY_FOR_PICKUP`.
  Адрес ПВЗ — объект, `city` приходит как «Москва,Москва». **Штрихкода в API нет.**
- WB — пути есть, токен без скоупа: 401 «token scope not allowed». Блокер, см. ниже.

**Фаза 2 — данные.** `migrations/300_mp_returns.sql` применена (серия **3xx** = поток `ret`):
`raw_mp_returns` (сырьё jsonb), `mp_returns` (шапка), `mp_return_items` (состав).
`returns_bot/{net,pending,collect}.py` + `returns_bot/sources/{ozon,yandex}.py`.
Идемпотентность проверена: два прогона подряд → те же числа, `first_seen` не съехал.
Пропал из выдачи API → `gone_at`, из сводки уходит.

**Фаза 3 — бот.** `returns_bot/{tg,codes,render,bot,daily_push}.py` + юниты
`returns-bot.service`, `returns-daily.{service,timer}`. Сводка отрисовывается и режется под
лимит Telegram (10 967 знаков → 4 части). **Не запущено: нет токена бота (см. «Нужно от Сергея»).**

## Текущая картина (прогон 02.08.2026)

Всего в базе 3564 возврата; открытых: **ЗАБРАТЬ 8** (Ozon Дисквэр 2, Яндекс 6),
**РАЗОБРАТЬСЯ 7**, **В ПУТИ 32**, остальное закрыто.

## Правила классификации — только в `returns_bot/pending.py`

Стадии `pickup` (забрать) / `transit` (едет) / `attention` (потерян/утилизация) / `closed`.
Ozon `Utilized`/`WriteOff` и Яндекс `EXPIRED`/`RECEIVED_FOR_EXPROPRIATION` — **closed**:
товара уже нет, действия невозможны, в ежедневный список они шум. Правка правил — только здесь.

## Нужно от Сергея

1. **Токен бота** у @BotFather → в `.env`: `TG_RETURNS_BOT_TOKEN`, `TG_RETURNS_ALLOWED_IDS`,
   `TG_RETURNS_NOTIFY_ID`. Отдельный токен обязателен: два бота на одном токене дерутся за
   `getUpdates` (у `invoice_bot` свой `TG_BOT_TOKEN`).
2. **WB: ОТДЕЛЬНЫЙ токен** со скоупами «Маркетплейс» + «Возвраты» на оба аккаунта →
   `WB_TOKEN_RETURNS_ACC1`, `WB_TOKEN_RETURNS_ACC2`. **Базовый токен не трогать** — перевыпуск
   базового 27.07 положил весь acc2 на 401 (память `project_mp_wb_token_scopes`).

## Запреты

- Только **чтение** API площадок. Ничего не согласовываем, не оспариваем, не заявляем на вывоз,
  в кабинеты не пишем.
- Витрины fin (`margin_by_sku`, `ms_return_cogs`) не трогаем: там деньги, здесь физика.
- **Свой QR из номера возврата не рисуем.** Только коды, которые площадка отдала сама
  (Ozon PNG/`logistic.barcode`). Если ПВЗ сканирует другой код — картинка вводит в заблуждение.
- Мерж в `main` — только по явному «ок» Сергея.

## Следующий шаг

Получить `TG_RETURNS_BOT_TOKEN` → разовая отправка `./venv/bin/python -m returns_bot.daily_push
--to <chat_id>` в чат Сергея → после ОК поставить юниты (`returns-daily.timer`, 06:05 UTC =
09:05 МСК; часы сервера UTC, systemd 249 таймзону в `OnCalendar` не понимает).
Затем фаза 4 — WB.

## Команды

```bash
./venv/bin/python -m returns_bot.collect --dry-run          # посчитать, в БД не писать
./venv/bin/python -m returns_bot.collect                    # собрать и записать
./venv/bin/python -m returns_bot.daily_push --dry-run --no-collect   # текст сводки в консоль
./venv/bin/python -m returns_bot.daily_push --to <chat_id>  # разовая отправка одному
python3 tools/territory_guard.py --status                   # домен сессии = ret
```
