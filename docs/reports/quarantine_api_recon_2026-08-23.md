# Карантин цен — разведка API (23.08.2026, поток mkt)

Проверено живыми вызовами по боевым ключам, read-only.

## Wildberries — ЕСТЬ чтение, выхода по API нет
- `GET discounts-prices-api.wildberries.ru/api/v2/quarantine/goods?limit&offset` → 200.
  Поля: `nmID, sizeID, techSizeName, newPrice, oldPrice, newDiscount, oldDiscount, priceDiff`.
- Сейчас в карантине: **wb_acc1 — 51**, **wb_acc2 — 33**. Все 84 nmID есть в нашей `wb_price`.
- Триггер WB: цена со скидкой **минимум втрое** ниже прежней (не 50 %).
- Выпуск: отдельного метода нет. Либо повторная загрузка цены (`POST /api/v2/upload/task`)
  ступенями меньше, чем втрое, либо руками в ЛК `/discount-and-prices/quarantine`.

## Яндекс.Маркет — ЕСТЬ и чтение, и выпуск по API
- `POST /businesses/{businessId}/price-quarantine` → 200. Поля: `offerId, currentPrice,
  lastValidPrice, verdicts[]`.
- Сейчас в карантине: **ya_acc1 — 88** позиций. Вердикты: `PRICE_CHANGE` 112, `LOW_PRICE` 2.
- Карантин живёт на уровне БИЗНЕСА; по трём кампаниям (`/campaigns/{id}/price-quarantine`)
  пусто — значит разбирать надо бизнес-список.
- Выпуск: `POST /businesses/{businessId}/price-quarantine/confirm`, `offerIds` 1..200 за вызов
  (проверено пустым payload → 400 с описанием лимита). Полностью автоматизируемо.
- Дисквэра на Маркете нет — только Цифровой.

## Ozon — ни чтения, ни выпуска по официальному API
- Ни один из 12 вероятных путей карантина не существует (все 404).
- `/v5/product/info/prices` и `/v3/product/info/list` признака карантина не содержат
  (в ответе нет ни поля, ни строки `quarantine`; `statuses.status='price_sent'` у здорового товара).
- По базе знаний Ozon разблокировка идёт ТОЛЬКО в ЛК и подтверждается кодом из SMS —
  поэтому публичного метода и нет. Автоматически выпустить товар на Ozon нельзя.

## Удержания МП (последний закрытый месяц — июль 2026), из витрин «Отчёты МП»
oz_acc1 57.9 % · oz_acc2 54.1 % · wb_acc1 55.4 % · wb_acc2 53.8 % · ya_acc1 73.3 %
(у Маркета в число входят реклама/бусты/подписка — кандидат на уточнение).
