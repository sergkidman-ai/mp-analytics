> **Обновлено после внедрения20.10:** этот документ описывает прежнюю подготовку.
> Текущий runtime (новые API, Money-читатели, остановлен старый PID) проверен и описан
> в [отчёте внедрения](../ozon_runtime_deploy_20261020/README.md).
> Единая база расходов опубликована: [отчёт](../ozon_expense_basis_20261020/README.md).

# Фактический статус совместимости перед live sync пилота

Проверка только чтением, 20.10.2026. Краткие структурированные результаты:
`compatibility_status.json`. Не запускались API-пробы, sync, финансы или рестарты.

**Финансовые/веб-читатели Money ещё НЕ задеплоены. Запись v4 в общий
raw_ozon_posting пока НЕ готова к совместному использованию.**

- Shared main = 5d5504f; reports/ozon_buyer_price.py, ozon_margin_control.py,
  web/app.py, web/marketing_app.py и shipment_guard/adapters.py ещё не используют
  подготовленную нормализацию. Один общий core/ozon_posting.py сам по себе читателей
  не переключает. Dashboard и marketing работают из shared /opt/mp-analytics.
- raw проверен по всем товарам каждой строки, не только первому: oz_acc1 8954 строк,
  oz_acc2 2433; Money price/customer_price пока 0. Неподдерживаемый формат пилот
  ещё не внес в общий источник.
- Старый run_daily PID 2681936 ещё жив и загружал старый collector. Он **не соблюдает
  новый advisory protocol**; наличие context manager в worktree пилота не делает
  этот старый процесс участником общей блокировки.

## Пилот и общий account-lock

Проверены commits fin-shipment-guard: базовый адаптер 4069230 (=513373c), блокировка
040fbc1 (=5fbfda1). В текущем sources.get_posting используется публичный account_lock
и request_page(..., check_lock=lease.check). Новые списки берут тот же lock автоматически.
Параметры sync timeout=120 совместимы с window_budget=120 (alias).
MS bigint lock 7348201701 и Ozon two-int account-lock независимы.

В текущем customer_guard/store.py ingest(..., mirror_raw=False) по умолчанию
сохраняет пилотные наблюдения без записи общего raw. Это подходящий режим изоляции
формата до reader deployment. Не выставлять mirror_raw=True/не зеркалировать
customer snapshots в raw_ozon_posting до отдельного подтверждения совместимости.
Сторона wb4c не запускала live sync и не меняла файлы worktree пилота.

## Условия снятия ограничения на общий raw

1. Применены и проверены все подготовленные читатели Money/legacy, включая веб и
   инструменты, с сохранением чужих изменений shared checkout.
2. Веб-процессы используют новую реализацию после согласованного reload/deployment.
3. Старый финансовый процесс завершён управляемо после повторной проверки состояния;
   следующий запускается с новым collector и общим account-lock. Не считать
   редактирование файлов обновлением уже импортированных функций.
4. Проверено чтение тестового Money и legacy всеми соответствующими путями,
   подтверждены currency/quantity; только затем включается зеркалирование в raw.

Подготовленные изменения совместимости остаются в worktree wb4c, прошли общий
набор 277 тестов. Это статус готовности к внедрению, не факт production deployment.
Полный deployment patch требует пересверки существующего untracked helper в shared
checkout; чужой файл не удалялся и не перезаписывался.

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
