> **Обновлено после внедрения20.10:** этот документ описывает прежнюю подготовку.
> Текущий runtime (новые API, Money-читатели, остановлен старый PID) проверен и описан
> в [отчёте внедрения](../ozon_runtime_deploy_20261020/README.md).
> Единая база расходов опубликована: [отчёт](../ozon_expense_basis_20261020/README.md).

# Координация wb4c → fin-shipment-guard

Адресат: диалог `01a11ab9-1be9-7063-b6e5-4155493a5490`.
Рабочая ветка wb4c: `fin/wb4c-ozon-api-migration`.
Корень: `/opt/mp-analytics/.claude/worktrees/fin-wb4c-ozon-api`.


## Стабильный общий API для пилота

**Актуальное дополнение: `5fbfda10c227a926624ad2912a757265091ccbc5`** поверх базового `513373c405e1f87df2e83eb91cfbcd001f03ccd6`.
Если базовый уже cherry-picked, применить только новый commit. Для нового checkout:

```bash
git cherry-pick 513373c405e1f87df2e83eb91cfbcd001f03ccd6 5fbfda10c227a926624ad2912a757265091ccbc5
```

```python
from collectors.ozon_postings import fetch_postings, account_lock
from core.ozon_posting import product_price

posts = fetch_postings('oz_acc2', 'fbs', date_from, date_to,
                       limit=20, max_pages=10, window_budget=120)
with account_lock('oz_acc2', timeout=2) as lease:
    lease.check()                  # перед КАЖДОЙ point-get HTTP-попыткой
    result = existing_point_get()  # собственный ограниченный HTTP timeout
```

Date interface сохранён; точные UTC timestamps также поддерживаются.
window_budget — общий бюджет окна с ожиданием lock, проверками и повторами;
старый timeout — alias, разные значения одновременно запрещены. fetch_postings
берёт общий per-account lock автоматически на все страницы. Point get обязан
использовать тот же context manager; напрямую отправленные HTTP он не перехватывает.
Новая зависимость: `core/ozon_posting_lock.py` (в коммите).
PG two-int ключи `(0x4F5A4F4E, 1/2)` не пересекаются с bigint MS `7348201701`.
OzonLockBusy — источник занят, checkpoint не продвигать; OzonLockLost — не продолжать
HTTP. Классы доступны из core.ozon_posting_lock. Вложенный same-thread/task контекст
переиспользует lease. MS → Ozon, не ждать MS/не писать документы, удерживая Ozon lock.

62 теста адаптера/lock прошли на чистом committed source, включая независимые
PostgreSQL-сессии в отдельном ТЕСТОВОМ namespace. Всего 277 тестов — OK. Таблицы,
MS lock, расписания, уведомления и marketplace данные не менялись; API-проб не было.
Финансовое внедрение по-прежнему требует совместимых читателей и нового процесса:
старый уже запущенный код advisory lock автоматически не приобретёт.


Базовый локальный commit **`513373c405e1f87df2e83eb91cfbcd001f03ccd6`**, 4 файла: collector, общий Money-парсер,
самодостаточные тесты, `docs/reference/ozon_postings_adapter.md`. Можно cherry-pick
в fin-shipment-guard без копирования кода и без остальных финансовых/маркетинговых
изменений wb4c. Commit не запушен, объект доступен в общем локальном Git-репозитории.

```python
from collectors.ozon_postings import fetch_postings
from core.ozon_posting import product_price

posts = fetch_postings(
    'oz_acc2', 'fbs', since_utc, until_utc,
    limit=20, max_pages=4, timeout=60,
    statuses=['awaiting_packaging', 'awaiting_deliver'], sort_dir='DESC',
)
```

since_utc/until_utc — aware datetime или ISO timestamps с Z/offset; границы точные,
включительные и фиксированные. Старые даты по-прежнему означают полные UTC-сутки.
max_pages/timeout — явные лимиты всего вызова; при превышении нет частичного
успешного возврата. Полнота означает только указанное окно и фильтр.
fetch_postings не пишет в raw/checkpoint, не запускает финансы/сторожа/уведомления.
product_price возвращает Decimal цены продавца за штуку, qty применить ровно один раз.

41 тест адаптера прошёл на **чистом committed source**, без незакоммиченных модулей.
Полный набор миграции: 256 тестов, включая READ ONLY SQL-фикстуры и контроль 3741.77 ₽.
Новых live-запросов для этой доработки не было; финансовые читатели Money остаются
отдельными изменениями wb4c. Этот коммит можно брать для пилота, а финансовый main
внедрять только вместе с совместимыми читателями.

## Готовые результаты: повторять пробы не нужно

- Схема типов ответа и проверенный request без чувствительных значений:
  `docs/reports/ozon_api_migration_20261020/FBS_V4_OBSERVED_SCHEMA.json`.
- Итоги уже выполненного прохода: `live_smoke.json` в той же папке.
  FBS oz_acc2 за 20.10: 22/22 уникальных, страницы 20+2. oz_acc1: 76/76,
  страницы 20+20+20+16. Сохранения в raw и изменений документов МС не было.
- Полный отчёт: `README.md`; это реализация в worktree, **ещё не production**.
- Код чтения/пагинации: `collectors/ozon_postings.py`.
- Общий парсер денег: `core/ozon_posting.py`, функция `product_price(product)`.
  При Money требует явную RUB; поддерживает исторический scalar/currency_code.
  Цена за штуку, для итога умножить на quantity один раз.

## status / sort: подтверждены пробой сторожа

Проверен `filter.statuses` (множественное число) как массив
`["awaiting_packaging", "awaiting_deliver"]` и `sort_dir="DESC"` в верхнем регистре.
В сохранённой первой странице фильтрованной пробы: 10 уникальных отправлений,
только эти два статуса, убывающий in_process_at, has_next=true. Это подтверждает
поведение на проверенной странице; не является полной выгрузкой отфильтрованного окна.

Отдельный **нефильтрованный** snapshot в том же артефакте полностью пройден:
34 уникальных отправления, страницы 20+14, has_next=false; присутствуют также
cancelled/delivered/delivering. Не выдавать число 34 за полный итог active-статусов.
Окно обоих запросов: 19.10 00:00 UTC — 20.10 13:18:03 UTC.

Источник проверки:
`/opt/mp-analytics/.claude/worktrees/fin-shipment-guard/docs/reports/customer_order_contract_20261020/evidence.json`,
ключи snapshot_body, pages, filter_probe. wb4c прочитал артефакт и проверил статусы,
сортировку, уникальность и завершённость; повторных API-запросов не делал.

Проверенное тело фильтрованной пробы:

```json
{
  "filter": {
    "since": "2026-10-19T00:00:00Z",
    "to": "2026-10-20T13:18:03Z",
    "statuses": ["awaiting_packaging", "awaiting_deliver"]
  },
  "limit": 10,
  "cursor": "",
  "with": {"financial_data": true},
  "sort_dir": "DESC"
}
```

Старые `filter.status` и `sort_dir="desc"` не давали нужного поведения. Финансовый
collector намеренно сохраняет все статусы, включая delivered/cancelled; активный
фильтр относится к лёгкому order-проходу. FBO параметры из этой FBS-пробы не выводить.

Ответ — корневые postings/has_next/cursor; result/offset больше не применяются.
Передавать курсор ровно из предыдущего ответа. Конец — has_next=false; для
has_next=true нужны непустая страница и продвигающийся курсор. Сопоставлять
financial_data.products с products по product_id=sku, не по позиции массива.

Money важен не только для products.price: customer_price тоже объект, причём
при цене продавца RUB покупатель бывает в KZT. Для подтверждения полной цены
отгрузки использовать products.price × quantity, а не customer_price. Старый
/v3/posting/fbs/get остаётся рабочим, его версию механически не повышали.
У FBO v3 в реальном delivered отсутствовали financial quantity/customer_price;
quantity восстанавливается по products и sku. Комиссия теперь объект
amount/percent/currency, финансовая цена price/old_price/payout в примерах — scalar.

## Разделение работ и инкрементальное обнаружение

Координацию принимаю: fin-shipment-guard продолжает контракт заказов/структуру
хранения, общий collector там не меняется. wb4c сохраняет владение миграцией общего
collector и совместимостью существующих читателей. Чужой worktree не редактировался.

Двух финансовых запусков в сутки недостаточно для обнаружения новых заказов.
**Текущая миграция ещё не реализует лёгкий инкрементальный опрос.** Не подключать
сторожа к повторному запуску financial main/run_daily.

Интеграционный контракт для следующего этапа:

1. Общий адаптер чтения Ozon, переиспользуемый финансовым и лёгким order-проходом;
   общие rate-limit/retry, Money, проверка курсора и полноты, без второго независимого
   полного финансового сборщика. Уже доступен прямой общий fetch_postings; транспорт
   остаётся внутри ozon_postings и используется через этот API, без копирования.
2. Общий raw-кэш с натуральным ключом account+posting_number и неизменённым payload.
   Финансовые period_from/period_to — параметры финансовой выборки, **не checkpoint
   обнаружения заказов и не признак полноты лёгкого прохода**. Отдельно хранить режим,
   фактические границы запроса, состояние/полноту прохода и свежесть наблюдения.
3. Для инкремента нужны точные timestamp-границы, отдельный checkpoint и окно
   перекрытия для задержанных/изменённых заказов. Теперь fetch_postings принимает точные timezone-aware timestamps. Сам API
   не хранит checkpoint и не выбирает overlap: это ответственность order-runner.
   Cursor действует только внутри неизменного окна, не долгосрочный watermark.
4. Checkpoint продвигается только после полной успешной пагинации и записи кэша;
   failed/partial не даёт ложной свежести. Оба читателя должны пользоваться общей
   политикой freshness и не перетирать более свежее наблюдение устаревшим снимком.
5. Фильтр since/to при обнаружении новых заказов не доказывает получение всех
   последующих изменений старых: открытые известные заказы нужно обновлять отдельно
   точечным get/проверенным способом. Отсутствие в новом окне не означает отмену.
6. Лёгкий runner обновляет источник/кэш и не строит финвитрины, не меняет статусы
   заказов/документы МС и не включает уведомления. Ритм пока не согласован: timer,
   cron, сторож/уведомления в этой координации не менялись.

Этот документ и схема созданы из имеющихся локальных доказательств; дополнительных
API-проб для передачи не делали. Канала прямой отправки в другой диалог среди
доступных инструментов нет; путь передачи оставлен в текущем диалоге и своём брифе.

## Координация после живого пилота: сохранить UI при deployment

Владелец сообщил: 37 posts в собственных snapshots, mirror_raw=False; расписания
и уведомления OFF, команды запускаются из fin-shipment-guard worktree. В shared
checkout опубликованы UI и additive core/ozon_posting.py; root collector не менялся.
Это не deployment финансовых читателей и не основание зеркалировать Money в общий raw.

Обязательные строки в shared web/app.py:

```python
from web.customer_guard_api import router as customer_guard_router  # поток: fin
app.include_router(customer_guard_router)
```

Страница `/warehouse/customer-orders` и API определены в web/customer_guard_api.py;
этот файл и web/static/customer_orders.html принадлежат пилоту и не заменяются
финансовым deployment. Прочие чужие изменения main также сохраняются.

Финансовый web-only patch проверен на текущем shared web/app.py:
apply --check=0, применение на временной копии и AST parse прошли, обе строки
сохранились. Shared файлы не менялись. Артефакты: web_reader_only.patch и
 deployment_preservation_check.json. Проверка не разрешает применять полный
финансовый patch без его отдельных актуальных preflight/reader checks.
