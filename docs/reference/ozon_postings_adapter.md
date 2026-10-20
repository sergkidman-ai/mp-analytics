# Общий адаптер постингов Ozon: API для customer-order пилота

Импорты без копирования реализации:

```python
from collectors.ozon_postings import fetch_postings
from core.ozon_posting import product_price, PostingProtocolError
```

## Публичный интерфейс

```python
fetch_postings(
    account, scheme, date_from, date_to, *,
    limit=1000, max_pages=None, timeout=900,
    statuses=None, sort_dir=None,
) -> list[dict]

product_price(product: dict) -> Decimal
```

- `date_from` / `date_to`: timezone-aware datetime или ISO timestamp с `Z`/offset.
  Нормализуются в UTC, сохраняют точное включительное окно. Naive timestamp запрещён.
  Прежние ISO-даты и date-объекты означают полные UTC-сутки; финансовый вызов по датам
  совместим. Граница окончания суток — 23:59:59.999 UTC.
- `limit`: целое 1..1000. Для пилота задавать **20** явно.
- `max_pages`: целое 1..10000, None означает 10000. Для пилота задавать собственный
  небольшой предел, например 4; неполное окно при достижении предела даёт ошибку.
- `timeout`: положительное конечное число секунд общего бюджета вызова. Остаток
  бюджета передаётся HTTP-повторам; учитываются Retry-After и паузы пагинации.
  Страница дополнительно ограничена 120 секундами и 5 HTTP-попытками.
- `statuses=None`: ВСЕ статусы. Для FBS можно передать непустой list/tuple строк;
  в HTTP это **filter.statuses**. В пробе подтверждены awaiting_packaging/awaiting_deliver.
  Каждый возвращённый статус проверяется на вхождение в запрошенный список.
- `sort_dir=None`: порядок не используется для вывода о полноте.
  Для FBS поддержан проверенный **"DESC"**; проверяется убывание in_process_at
  внутри страниц и между ними. Другие значения пока не открыты этим интерфейсом.
  Для FBO status/sort запрещены до отдельной проверки контракта.
- Возвращается полный список исходных posting dict. Никаких записей в БД, обновления
  cache/checkpoint, сообщений, документов МС, финансовых витрин или вызова main.
  При transport/schema/currency/page/time ошибке частичного успешного результата нет.
- Пагинация курсорная; фильтр и окно не меняются. Циклы курсоров, дубли,
  неожиданные статусы, нарушение сортировки и выход даты за окно дают ошибку.
- Ошибки параметров: ValueError. Контракта/полноты: PostingProtocolError
  (подкласс ValueError). Бюджета времени: TimeoutError. Повторов: RuntimeError.
  Невременные HTTP-ошибки: requests.HTTPError.

## Пример ограниченного чтения

```python
from datetime import datetime, timezone, timedelta
from collectors.ozon_postings import fetch_postings
from core.ozon_posting import product_price

end = datetime.now(timezone.utc)        # зафиксировать один раз до первого запроса
start = end - timedelta(hours=2)
posts = fetch_postings(
    'oz_acc2', 'fbs', start, end,
    limit=20, max_pages=4, timeout=60,
    statuses=['awaiting_packaging', 'awaiting_deliver'],
    sort_dir='DESC',
)
for posting in posts:
    for product in posting['products']:
        seller_total = product_price(product) * product['quantity']
```

Если нужен полный снимок всех статусов, убрать statuses/sort_dir и сохранить это
различие в метаданных прохода. Конечная страница означает полноту именно переданных
фильтров и окна, а не всех заказов кабинета. Здесь нет скользящего checkpoint;
структура хранения/overlap/freshness и обновление старых открытых заказов принадлежат
customer-order runner. Дата in_process_at новых заказов не является watermark всех
последующих изменений заказа.

`product_price` возвращает цену продавца **за штуку**, Decimal без округления;
quantity учитывается вызывающим кодом ровно один раз. Поддерживает Money
{amount,currency} и исторический scalar с currency_code. У Money явная RUB обязательна,
у legacy scalar без currency_code сохранено историческое соглашение RUB.
Не использовать customer_price для полной суммы продавца: у покупателя может быть KZT
при цене продавца RUB. Исходный словарь не меняется.

## Интеграция и границы коммита

Отдельный коммит содержит только `collectors/ozon_postings.py`, `core/ozon_posting.py`,
самодостаточные `tests/test_ozon_postings_adapter.py` и этот документ. Он не содержит
код customer-order runner, схемы БД пилота или изменения его адаптера/служб.
Подключение — cherry-pick этого коммита в собственный worktree; код не копировать.
Проверка адаптера:

```bash
./venv/bin/python -m unittest tests.test_ozon_postings_adapter -q
```

Финансовая миграция потребителей Money находится отдельными изменениями wb4c.
До совместного внедрения финансовых читателей не запускать из одного этого коммита
финансовый main/run_daily: новые raw-форматы требуют совместимых потребителей.
Пилот использует только fetch_postings, отдельно управляет сохранением кэша и не
пересобирает финансы. Расписание/уведомления здесь не устанавливаются.
