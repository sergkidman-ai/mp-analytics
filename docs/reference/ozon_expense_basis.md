# Единая база расходов Ozon для акций, отгрузок и customer-order

## Контракт

```python
from core.ozon_expense_basis import (
    get_basis, basis_status, shipment_rule, floor_price,
    floor_from_retention, BasisUnavailable,
)
basis = get_basis('oz_acc2')          # только актуальная подтверждённая база
floor = floor_price(full_cost, sold_units, basis)  # Decimal, копейки
rule = shipment_rule('oz_acc2')      # совместимая ms_shipment_rules, та же version
state = basis_status('oz_acc2')      # current + last_usable, без тихого fallback
```

Одинаковые account/cost/sold_units/version дают один пол. Quantity — число продаваемых
единиц конкретного инструмента: один набор не превращается в число его компонентов.
Цель неизменна: max(300 ₽ × sold_units, 10% полной себестоимости). Потеря относительно
FIFO и непрохождение целевого пола — разные сигналы. Округление только конечного пола
до копейки, ROUND_HALF_EVEN, расчёт Decimal precision=38; keep не урезается до 4 знаков.

`BasisUnavailable` — нет текущей подтверждённой базы, закрытый месяц ещё не подтверждён,
ошибка источника/публикации или версия правила не совпала. В этом случае не применять
константу и не использовать август как актуальный. basis_status показывает датированную
последнюю пригодную версию, когда текущая недоступна; get_basis по умолчанию её не подставляет.

Для customer-order заменить локальное получение ms_shipment_rules на shipment_rule,
а локальную формулу пола на floor_price(..., basis) / floor_from_retention(...).
Импортировать общий код, не копировать. Исторические проверки сохраняют исходную
version/rule и результат: новые версии используются только в новой проверке.
Worktree customer-order этой сессией не меняется; интеграция — владельцем пилота.

## Источник и полнота

Предыдущий календарный месяц — только кандидат. Требуются одновременно:

- лог успешно завершённого полного календарного by-day collector с итоговой суммой,
  всеми днями (включая нулевые), совпадением дневных count/amount с текущим raw;
- совпадение по всем IDs/суммам и дням raw_ozon_accrual и raw_ozon_transaction;
- закрытый отчёт raw_ozon_realization с нужными start_date/stop_date, непустыми rows,
  валютой RUB; открытый/пустой период не принимается;
- положительные продажи, допустимая доля расходов и сходимость разложения с net.

Одного наличия raw-строк недостаточно. Недостающий лог/отчёт/день/ошибка — явный
incomplete. Ошибка постингов не делает уже успешно собранные расходы неполными:
постинги не являются источником выручки/баллов/партнёрского split.

Состав как у действующего EXP_SVC: commission, delivery, partners, fbo, promo,
penalty, unclassified. Знаки корректировок сохраняются: возврат комиссии/услуги
уменьшает расходы. Не суммировать abs каждой операции/кредитовой строки.
Returns (сторно продаж), compensation и other-income отражаются отдельно, не
вычитаются второй раз из числителя. Классификация — только существующий реестр
ops.ozon_lines_registry; неизвестные коды явно указаны и включены в unclassified.
Финансовые витрины/классификатор не переписываются ради совпадения пола.

Это доля всего кабинета, включая FBO-расходы, рекламу и фиксированные подписки.
Она не является фактической комиссией FBS-заказа. Переход к FBS-only/SKU-модели,
исключение рекламы/FBO/фиксированных услуг — отдельное изменение экономической
политики, здесь не выполняется. gross_debits/gross_credits и fixed_subscription_expense
в версии помогают проверить состав без изменения правил.

## Хранение и обновление

Миграция `migrations/074_ozon_expense_basis.sql` (073 занят пилотом).
fin_ozon_expense_basis — неизменяемые версии, запрет UPDATE/DELETE;
fin_ozon_expense_current — текущий проверенный результат кандидата;
fin_ozon_rule_history — старые ms_shipment_rules целиком до UPDATE/DELETE.
В version входит hash исходников/состава/периода/сумм/доказательств полноты;
повтор неизменного refresh идемпотентен. Исправление данных создаёт новую версию.

```bash
# только уже собранные источники, без МС/MP API и пересборки витрин
./venv/bin/python -m ops.ozon_expense_basis refresh > /tmp/ozon-basis-preview.jsonl
./venv/bin/python -m ops.ozon_expense_basis refresh --apply > /tmp/ozon-basis-published.jsonl
./venv/bin/python -m ops.ozon_expense_basis status
```

Публикация атомарна: snapshot, pointer, архив прежнего правила, обновление существующих
Ozon ms_shipment_rules. Другие платформы/organization/enabled не меняются. Правило
истекает в конце месяца, следующего за базовым. При incomplete прежняя версия
сохраняется в истории, действующее правило помечается просроченным, не используется
молча. Первоначальное создание отсутствующего правила делает штатный bootstrap из
общей подтверждённой базы; статический август для Ozon из bootstrap удалён.

После Ozon-этапов обычного run_daily добавлено локальное обновление базы; новый cron,
таймер, MS-запросы и рассылка не создаются. Полный run_daily ради проверки не запускать.
Не запускать боевые action/shipment guards ради smoke этой библиотеки.

Тесты:

```bash
OZON_POSTING_SQL_TEST=1 ./venv/bin/python -m unittest tests.test_ozon_expense_basis \
  tests.test_ozon_promo_bundle_floor tests.test_ozon_actions_v2 -q
```

DB-тесты создают отдельную тестовую схему в транзакции и откатывают её; проверяют
историю/immutable snapshots/idempotence/явный incomplete, не меняют рабочие правила.
