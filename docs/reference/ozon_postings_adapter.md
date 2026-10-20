# Общий адаптер постингов Ozon: API для customer-order пилота

Импорты без копирования реализации:

```python
from collectors.ozon_postings import fetch_postings, account_lock
from core.ozon_posting import product_price, PostingProtocolError
```

## Публичный интерфейс

```python
fetch_postings(
    account, scheme, date_from, date_to, *,
    limit=1000, max_pages=None, timeout=None, window_budget=None, lock_timeout=5,
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
- `window_budget`: положительное конечное число секунд общего бюджета вызова.
  Для пилота **120**; остаток учитывает ожидание account-lock, его проверки,
  HTTP-попытки, Retry-After и паузы пагинации. `timeout` — совместимый alias; если
  переданы оба с разными значениями, ValueError. По умолчанию бюджет 900 секунд.
  Страница дополнительно ограничена 120 секундами и 5 HTTP-попытками.
- `lock_timeout`: ожидание занятого account-lock (по умолчанию 5 секунд, не больше
  бюджета окна). 0 — одна попытка без ожидания занятости. Подключение к PostgreSQL
  имеет отдельный connect_timeout до 5 секунд и ограниченные SQL-запросы; после
  подключения истёкший бюджет окна не позволяет начать HTTP.
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
    limit=20, max_pages=10, window_budget=120,
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

## Общая блокировка списков и point get

`fetch_postings` автоматически держит session advisory lock одного кабинета
на всё окно, включая все страницы и повторы; у него нет флага обхода блокировки.
Ключи — PostgreSQL two-int `(0x4F5A4F4E, 1)` для oz_acc1 и `(0x4F5A4F4E, 2)` для
oz_acc2. Это отдельное пространство от bigint MS lock `7348201701`.
Разные кабинеты не блокируют друг друга. Миграция БД не нужна.

Для point get использовать тот же публичный context manager:

```python
from collectors.ozon_postings import account_lock
from core.ozon_posting_lock import OzonLockBusy, OzonLockLost

with account_lock('oz_acc2', timeout=2) as lease:
    lease.check()                  # непосредственно перед HTTP
    result = existing_point_get()  # собственный ограниченный HTTP timeout
```

При повторах point get проверять `lease.check()` перед **каждой** HTTP-попыткой.
Если используется общий низкоуровневый `request_page`, ему можно передать
`check_lock=lease.check, deadline=<monotonic deadline>` — там проверка встроена
перед каждой попыткой. Не переносить реализацию lock в пилот.

`OzonLockBusy` (TimeoutError) — другой участник удерживает account-lock, проход
не состоялся и checkpoint не продвигается. `OzonLockLost` (RuntimeError) — потеря
владения/DB-соединения; дальнейшие HTTP-попытки запрещены. Старые процессы/вызовы,
которые не используют этот протокол, advisory lock не останавливает: финансовый
процесс должен быть запущен с новым кодом, point get обязан брать этот контекст.

Lock живёт на отдельном autocommit-соединении, без долгой открытой транзакции;
finally освобождает и закрывает его также при исключениях. Вложенный вызов одного
кабинета в том же thread/task переиспользует lease; потомок async task/другой поток
не получает обхода через унаследованный контекст. Можно обернуть общий проход
account_lock и вызвать fetch_postings внутри, он повторно тот же lock не захватывает.

MS LOCK_ID остаётся у shipment worker. Порядок владения: MS → Ozon, если нужны оба;
не ждать MS-lock и не писать MS-документы, удерживая Ozon-lock.

Семантика session locks и двух независимых пространств ключей:
[официальная документация PostgreSQL 16](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS).

## Интеграция и границы коммита

Базовый commit `513373c405e1f87df2e83eb91cfbcd001f03ccd6` содержит collector,
`core/ozon_posting.py`, самодостаточные тесты адаптера и этот документ. Следующий
commit расширяет API и добавляет зависимость `core/ozon_posting_lock.py` и тесты lock.
Для нового checkout нужны оба последовательных коммита; если базовый уже cherry-picked,
применяется только новый. Их точные hashes перечислены в передаче wb4c. Он не содержит
код customer-order runner, схемы БД пилота или изменения его адаптера/служб.
Подключение — cherry-pick этого коммита в собственный worktree; код не копировать.
Проверка адаптера:

```bash
./venv/bin/python -m unittest tests.test_ozon_postings_adapter tests.test_ozon_posting_lock -q
```

Финансовая миграция потребителей Money находится отдельными изменениями wb4c.
До совместного внедрения финансовых читателей не запускать из одного этого коммита
финансовый main/run_daily: новые raw-форматы требуют совместимых потребителей.
Пилот использует только fetch_postings, отдельно управляет сохранением кэша и не
пересобирает финансы. Расписание/уведомления здесь не устанавливаются.
