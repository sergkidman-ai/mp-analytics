# Ozon: accrual/by-day → формат transaction/list (сопоставление полей)

Дата: 2026-09-22. Только анализ: в репо и БД ничего не менялось, платные API не вызывались.
Выборка: `raw_ozon_accrual` ⋈ `raw_ozon_transaction` по `account + accrual_id = operation_id`,
`accrual_date` 01.08–07.09.2026, оба аккаунта: **6986 пар** (acc1 5623, acc2 1363).
Сопоставление полное в обе стороны: за период нет ни транзакции без начисления, ни начисления без транзакции.
Машиночитаемые таблицы: `docs/reference/ozon_accrual_to_txn_mapping.json`.

Проверка выполнена прототипом адаптера (лежит вне репо, в scratchpad сессии): он собирает payload старого
формата из начисления, после чего каждое поле и результаты `categorize_operation` и `_balance_range`
(логика `ozon_mp_report`) сравниваются с оригинальной транзакцией.

## 1. Потребители `raw_ozon_transaction` и читаемые поля

| Файл | Что читает из payload (и колонки таблицы) |
|---|---|
| `collectors/ozon.py` | пишет таблицу; `categorize_operation`: accruals_for_sale, sale_commission, delivery_charge, return_delivery_charge, services[].name/price, amount, operation_type_name (фолбэк operation_type) — классификатор остатка, если tracked==0 |
| `web/app.py` | `_oz_ops/_oz_aggregate`: всё, что читает `categorize_operation`, плюс items (есть/нет), posting.delivery_schema; сводка сырых типов: operation_type_name/operation_type, amount; items[].sku/name (справочник названий, 3 места); число записей items[] при accruals_for_sale>0 = штуки; недельные accruals_for_sale/amount по operation_date; DISTINCT account |
| `reports/ozon_mp_report.py` (+ ozon_mp_freeze/page, mp_numbers, run_daily, ozon_stock_action, halo_eval через импорт) | `_balance_range`: accruals_for_sale, sale_commission, amount, services[].name/price, operation_type (остаток → RESID_OPS; подписки OperationSubscription*); заказы/возвраты: posting.posting_number, accruals_for_sale, operation_type; max(operation_date) |
| `ops/ozon_lines_audit.py` | amount, accruals_for_sale, sale_commission, services[].name/price, operation_type, operation_date |
| `reports/ozon_expenses.py` | `categorize_operation` + posting.delivery_schema (разрез расходов по схеме), operation_date |
| `reports/margin_ozon_sku.py` | `categorize_operation`, accruals_for_sale, items[].sku/name (кратность = штуки), posting.posting_number, **posting.order_date** (день заказа → дата FIFO себеста, фолбэк operation_date), posting.delivery_schema, operation_type (DeliveredToCustomer / StornoDeliveredToCustomer), operation_date; история аккаунта целиком |
| `collectors/ms_demand_cogs.py` | posting.posting_number при operation_type=OperationAgentDeliveredToCustomer |
| `reports/mp_cogs_drill.py` | posting.posting_number, accruals_for_sale, operation_type (Storno), items[].sku, operation_date |
| `reports/ozon_margin_control.py` | services[] name=MarketplaceServiceItemDirectFlowLogistic, price; posting.posting_number; amount; accruals_for_sale; operation_type_name (фильтры по русским именам, 'Доставка покупателю'); operation_date |
| `ops/ozon_promo_bonus_watch.py` | operation_type, operation_type_name (ILIKE по словам), amount, account; **колонка period_from** |
| `tools/ozon_acc1_plan.py`, `ozon_acc1_plan2.py` | items[].sku, accruals_for_sale, operation_type LIKE '%DeliveredToCustomer%', operation_date |
| `tools/ozon_halo_eval.py`, `tools/ozon_eval_core.py` | только **колонка loaded_at** (свежесть данных) |
| `docs/reports/ozon_decline_2026-09-14/queries/*` (разовые выгрузки) | operation_date, operation_type(_name), amount, accruals_for_sale, sale_commission, delivery_charge, return_delivery_charge, items[].sku, posting.posting_number, operation_id |

Поле `type` (orders/returns/services…) и `posting.warehouse_id` никто не читает.

## 2. Форма начисления и ключ классификации

`accrued_category`: ITEM 3368 (эквайринг, подписка Premium %, Звёздные, упаковка, вывоз FBO и т.п.),
POSTING 1958 (доставка/возврат отправления), NON_ITEM 1660 (клики, штрафы, rFBS-агентские, хранение и т.п.).
`container_fees` за период пуст везде.

Сумма по видам начислений (type_id) берётся из: `item_fees.fees[].fees[]`, `non_item_fee`,
`posting.products[].delivery.services[]`. Ключ = `категория|отсортированный набор type_id|знак комиссии`
(`c+` sale_amount>0, `c-` <0, пусто = commission null).

### 2.1 Ключ → operation_type (56 ключей, 33 operation_type)

| ключ | operation_type | operation_type_name | пар |
|---|---|---|---|
| `ITEM|15|` | DisposalOfGoods | Утилизация товара | 2 |
| `ITEM|1|` | MarketplaceRedistributionOfAcquiringOperation | Оплата эквайринга | 1623 |
| `ITEM|38|` | OperationMarketplacePackageMaterialsProvision | Обеспечение материалами для упаковки товара | 27 |
| `ITEM|39|` | OperationMarketplacePackageRedistribution | Упаковка товара партнёрами | 27 |
| `ITEM|51|` | PremiumMembership | Подписка Premium Pro (процент) | 903 |
| `ITEM|71|` | SellerReturnsDeliveryToPickupPoint | Вывоз товара со Склада силами Ozon: Доставка до ПВЗ | 30 |
| `ITEM|71|` | OperationSellerReturnsCargoAssortmentValid | Подготовка товара к вывозу: Валид | 29 |
| `ITEM|71|` | OperationSellerReturnsCargoAssortmentInvalid | Подготовка товара к вывозу: Брак | 1 |
| `ITEM|74|` | StarsMembership | Звёздные товары | 692 |
| `ITEM|79|` | OperationMarketplaceItemTemporaryStorageRedistribution | Временное размещение товара партнерами | 31 |
| `ITEM|82|` | OperationMarketplaceServiceVolumeWeightCharacsProcessing | Услуга по дополнительной обработке ОВХ | 3 |
| `NON_ITEM|121|` | OperationCourierPickUpReinvoice | Доставка до СЦ партнёрами | 34 |
| `NON_ITEM|18|` | OperationMarketplaceServiceEarlyPaymentAccrual | Услуга досрочной выплаты | 10 |
| `NON_ITEM|25|` | AccrualInternalClaim | Потеря по вине Ozon в логистике | 2 |
| `NON_ITEM|25|` | AccrualWithoutDocs | Начисление по спору | 1 |
| `NON_ITEM|38|` | OperationMarketplacePackageMaterialsProvision | Обеспечение материалами для упаковки товара | 1 |
| `NON_ITEM|39|` | OperationMarketplacePackageRedistribution | Упаковка товара партнёрами | 1 |
| `NON_ITEM|41|` | OperationMarketplaceCostPerClick | Оплата за клик | 495 |
| `NON_ITEM|43|` | OperationCourierArrangement | Организация выезда курьера | 23 |
| `NON_ITEM|44|` | OperationCourierPickUpDelivery | Доставка курьером Pick-up | 23 |
| `NON_ITEM|46|` | OperationMarketplaceServiceStorage | Услуга размещения товаров на складе | 70 |
| `NON_ITEM|52|` | OperationSubscriptionPremiumPlus | Подписка Premium Plus | 3 |
| `NON_ITEM|54|` | OperationPromotionWithCostPerOrder | Продвижение с оплатой за заказ | 75 |
| `NON_ITEM|62|` | MarketplaceSellerReexposureDeliveryReturnOperation | Перечисление за доставку от покупателя | 168 |
| `NON_ITEM|63|` | MarketplaceAgencyFeeAggregator3plRFBS | Агентское вознаграждение за доставку Партнерами Ozon на схеме realFBS | 177 |
| `NON_ITEM|64|` | MarketplaceServiceRedistributionOfDeliveryServicesRFBS | Услуги доставки Партнерами Ozon на схеме realFBS | 177 |
| `NON_ITEM|65|` | MarketplaceServiceEasyReturnRfbs | Услуги доставки Партнёрами Ozon на схеме realFBS: Лёгкий возврат Почтой России | 4 |
| `NON_ITEM|93|` | DefectFineShipmentDelay | Превышение индекса ошибок: просроченная отгрузка | 17 |
| `NON_ITEM|94|` | DefectFineShipmentDelayRated | Отгрузка в нерекомендованный слот | 299 |
| `NON_ITEM|94|` | DefectFineShipmentDelayRatedCancelled | Отгрузка в нерекомендованный слот - отмена начисления | 11 |
| `NON_ITEM|96|` | OperationMarketplaceAcceleratedProductReviews | Ускоренный сбор отзывов | 69 |
| `POSTING|16,17,29,32|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 3 |
| `POSTING|16,17|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 3 |
| `POSTING|17,29,32,42|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 1 |
| `POSTING|17,29,32|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 3 |
| `POSTING|17,29,32|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 299 |
| `POSTING|17,32,98|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 4 |
| `POSTING|17,32,98|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 44 |
| `POSTING|17,32|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 15 |
| `POSTING|17|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 10 |
| `POSTING|29,32,42|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 5 |
| `POSTING|29,32,42|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 516 |
| `POSTING|29,32|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 9 |
| `POSTING|29,32|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 302 |
| `POSTING|29|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 45 |
| `POSTING|32,42,98|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 23 |
| `POSTING|32,42,98|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 122 |
| `POSTING|32,42,98|c-` | OperationAgentStornoDeliveredToCustomer | Доставка покупателю — отмена начисления | 1 |
| `POSTING|32,42|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 31 |
| `POSTING|32,98|` | OperationAgentDeliveredToCustomer | Доставка покупателю | 9 |
| `POSTING|32,98|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 44 |
| `POSTING|32,98|c-` | OperationAgentStornoDeliveredToCustomer | Доставка покупателю — отмена начисления | 1 |
| `POSTING|32|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 61 |
| `POSTING|32|c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 19 |
| `POSTING|32|c-` | OperationAgentStornoDeliveredToCustomer | Доставка покупателю — отмена начисления | 1 |
| `POSTING|42|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 36 |
| `POSTING|45|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 46 |
| `POSTING|59|` | OperationReturnGoodsFBSofRMS | Доставка и обработка возврата, отмены, невыкупа | 101 |
| `POSTING||c+` | OperationAgentDeliveredToCustomer | Доставка покупателю | 168 |
| `POSTING||c-` | ClientReturnAgentOperation | Получение возврата, отмены, невыкупа от покупателя | 36 |
**Правила вместо таблицы для POSTING** (в выборке без исключений):
- commission есть, sale_amount>0 → `OperationAgentDeliveredToCustomer`;
- commission есть, sale_amount<0: есть услуги доставки → `OperationAgentStornoDeliveredToCustomer`, нет → `ClientReturnAgentOperation`;
- commission null: 32 (Logistic) в наборе и type_id ≥2 → `OperationAgentDeliveredToCustomer` (доставка без выручки в этой операции, 57 шт.), иначе → `OperationReturnGoodsFBSofRMS`. Правило эмпирическое; для `POSTING|32|` (одна логистика) все 61 — возврат.

**ITEM и NON_ITEM**: type_id → operation_type один к одному, кроме:

| type_id | неоднозначность | пар | развязка |
|---|---|---|---|
| 94 NON_ITEM | DefectFineShipmentDelayRated 299 / …Cancelled 11 | 310 | **по знаку суммы** (<0 штраф, >0 отмена) → 100 % |
| 51 ITEM | имя услуги PremiumMembershipCommission 901 / …Cancelled 2 | 903 | **по знаку** → 100 % |
| 71 ITEM (SellerReturns) | OperationSellerReturnsCargoAssortmentValid 29 / …Invalid 1 / SellerReturnsDeliveryToPickupPoint 30; имя услуги соответственно MarketplaceServiceSellerReturnsCargoAssortment либо MarketplaceServiceProductMovementFromWarehouse | 60 | **не развязывается**: суммы за единицу пересекаются (30/35/45 есть у обоих), коробка `sr01-bx…` общая. Итого −4 120 ₽ |
| 25 NON_ITEM (ItemCompensation) | AccrualInternalClaim 2 / AccrualWithoutDocs 1 | 3 | **не развязывается** (обе с плюсом). 111 328,43 ₽, из них WithoutDocs 51 571,08 ₽ |
| 16 POSTING (Drop-Off) | имя услуги MarketplaceServiceItemDropoffPVZ 4 / …DropoffSC 2 | 6 | не развязывается (признака нет) |

`operation_type_name` и `type` — функции от `operation_type` (по всей истории `raw_ozon_transaction` ни одного исключения).
Для типов, которых нет в истории, нужен фолбэк: `description` из `ozon_accrual_types.json`.

## 3. Денежные поля

| поле старого формата | источник в начислении | точных совпадений (±0,005) |
|---|---|---|
| amount | total_amount.amount | 6986/6986 (100 %) |
| accruals_for_sale | Σ posting.products[].commission.sale_amount (0, если commission null) | 6986/6986 (100 %); seller_price совпадает только в 6867 случаях, **не использовать** |
| sale_commission | Σ products[].commission.sale_commission (= commission.commission) | 6986/6986 (100 %) |
| delivery_charge, return_delivery_charge | — | в выборке всегда 0 (6986/6986); адаптер пишет 0 |
| services[] | fees ITEM/POSTING, по type_id, сгруппированные по операции | все 20 имён услуг сопоставлены (0 несопоставленных); сумма по имени совпадает в 6954/6986 (99,54 %) |
| остаток (amount − accr − comm − Σservices) | non_item_fee | у NON_ITEM в старом формате services всегда пусты (1660/1660), сумма уходит в остаток → адаптер services не заполняет. У ITEM/POSTING остатка нет |

commission.bonus / coinvestment в старом формате отдельно не видны (bonus входит в sale_amount).

### 3.1 type_id → services[].name

| type_id | имя вида | services[].name | пар |
|---|---|---|---|
| 1 | Acquiring | MarketplaceRedistributionOfAcquiringOperation | 1623 |
| 32 | Logistic | MarketplaceServiceItemDirectFlowLogistic | 1513 |
| 29 | LastMileCourier | MarketplaceServiceItemRedistributionLastMileCourier | 1183 |
| 51 | PremiumMembership | PremiumMembershipCommission | 901 |
| 42 | Pick-Up | MarketplaceServiceItemDropoffPickup | 735 |
| 74 | StarsMembership | ItemAgentServiceStarsMembership | 692 |
| 17 | Drop-Off Agent | MarketplaceServiceItemRedistributionDropOffApvz | 382 |
| 98 | DeliveryToHandoverPlaceByOzon | MarketplaceServiceItemDeliveryToHandoverPlaceOzon | 248 |
| 59 | ReturnFlowLogistic | MarketplaceServiceItemReturnFlowLogistic | 101 |
| 45 | PickUpPointReturnAcceptance | MarketplaceServiceItemRedistributionReturnsPVZ | 46 |
| 79 | TemporaryPlacementsAgent | MarketplaceServiceItemTemporaryStorageRedistribution | 31 |
| 71 | SellerReturns | MarketplaceServiceSellerReturnsCargoAssortment | 30 |
| 71 | SellerReturns | MarketplaceServiceProductMovementFromWarehouse | 30 |
| 38 | PackageCost | MarketplaceServiceItemPackageMaterialsProvision | 27 |
| 39 | PackingFee | MarketplaceServiceItemPackageRedistribution | 27 |
| 16 | Drop-Off | MarketplaceServiceItemDropoffPVZ | 4 |
| 82 | VolumeWeightCharacteristicsProcessing | MarketplaceServiceVolumeWeightCharacsProcessing | 3 |
| 16 | Drop-Off | MarketplaceServiceItemDropoffSC | 2 |
| 51 | PremiumMembership | PremiumMembershipCommissionCancelled | 2 |
| 15 | Disposal | MarketplaceServiceItemDisposalDetailed | 2 |
## 4. Поля товара, отправления и дат

| поле | как восстановить | результат |
|---|---|---|
| operation_date | `date + ' 00:00:00'` (в старом формате время всегда 00:00:00) | 100 % |
| operation_id | accrual_id | 100 % |
| items[].sku (с повтором по штукам) | POSTING: products[].sku×quantity; ITEM: item_fees.fees[].sku×quantity; NON_ITEM: [] | мультимножество совпало 6986/6986 (100 %) |
| items[].name | `ozon_product.name` по (account, sku) | 6316/6316 записей (100 %) |
| posting.posting_number | unit_number (NON_ITEM тоже: номер отправления, кампании и т.п.) | 100 % |
| posting.delivery_schema | POSTING: posting.delivery_schema.upper() (Fbs→FBS); ITEM/NON_ITEM: `raw_ozon_posting` по unit_number: scheme fbo→FBO, tpl_integration_type aggregator/hybrid→RFBS, иначе FBS; нет строки → '' | 100 % (пустое ⇔ нет строки в raw_ozon_posting). Зависит от полноты raw_ozon_posting |
| posting.order_date | **в начислении нет.** Лучший суррогат: `raw_ozon_posting.in_process_at` + 3 ч | точно 3 % (время расходится на секунды); **день** совпадает у 91 % POSTING (1780/1957), по всем операциям 61 %; эквайринг/подписки несут номер заказа, а не отправления, для них даты нет. `raw_ozon_orders_report.processed_at` хуже (83 %) |
| posting.warehouse_id, type | не восстанавливается / из operation_type | никто не читает |
| колонки period_from, loaded_at | заполняет адаптер (период запроса, now()) | нужны для promo_bonus_watch и halo/eval_core |

## 5. Сквозная проверка прототипа адаптера (6986 пар)

| проверка | совпало | % |
|---|---|---|
| operation_type | 6955 | 99.556 % |
| operation_type_name | 6955 | 99.556 % |
| type | 6986 | 100.0 % |
| amount | 6986 | 100.0 % |
| accruals_for_sale | 6986 | 100.0 % |
| sale_commission | 6986 | 100.0 % |
| delivery_charge | 6986 | 100.0 % |
| services_by_name | 6954 | 99.542 % |
| items_sku | 6986 | 100.0 % |
| items_name | 6986 | 100.0 % |
| posting_number | 6986 | 100.0 % |
| delivery_schema | 6986 | 100.0 % |
| order_date_exact | 232 | 3.321 % |
| order_date_day | 4275 | 61.194 % |
| operation_date | 6986 | 100.0 % |
| categorize_operation | 6955 | 99.556 % |
| mp_report_lines | 6953 | 99.528 % |
| mp_report_lines_money | 6986 | 100.0 % |
- `mp_report_lines_money` — строки баланса ЛК из `_balance_range` (ozon_mp_report, lines_audit): **100 % до копейки**.
  В детализации (`mp_report_lines`, ключ строка+имя услуги) 33 операции расходятся по подстроке:
  71 (fbo: вывоз против подготовки к вывозу), 16 (delivery: Drop-off ПВЗ против СЦ), 25 (compensation: имя в ЛК).
- `categorize_operation` (web/app, ozon_expenses, margin_ozon_sku): итог операции совпадает всегда. Расходятся **31 операция**:
  - 30 шт. type 71: `MarketplaceServiceSellerReturnsCargoAssortment` содержит «return» → категория returns, а
    `ProductMovementFromWarehouse` → fbo. Адаптер с выбором по большинству переносит **1 868 ₽ из returns в fbo**;
  - 1 шт. type 25: `_classify_optype` ('Начисление по спору' → other, 'Потеря по вине Ozon' → compensation): **51 571,08 ₽ other↔compensation**.
- operation_type/operation_type_name: 99,56 %, промахи — те же 31 операция (type 71 и 25).
- order_date: см. п. 4. Это единственное поле, которое нельзя восстановить в принципе.

## 6. Вердикт

**Адаптер жизнеспособен.** Итоги, выручка, комиссия, услуги, штуки, SKU и названия, отправления, схема и дата операции
совпадают на 100 %. Официальный баланс (`ozon_mp_report`) сходится до копейки. Условия:

1. Таблицы type_id→имя услуги и ключ→operation_type обучены на этой выборке: 20 имён услуг и 33 operation_type.
   В справочнике около 120 type_id; новый type_id получит имя `TYPE_<id>`, а в отчёте упадёт в «Неклассифицировано»,
   как и сейчас с незнакомыми услугами. Реестр `ops/ozon_lines_registry.py` нужно будет дополнить.
2. Не восстанавливаются без потерь:
   - **posting.order_date.** Нужен margin_ozon_sku (день заказа → дата FIFO себеста; при пустом поле берётся operation_date). Суррогат `in_process_at+3ч` даёт верный день примерно в 91 % случаев;
   - **operation_type у type 71 и 25** (63 операции). Страдают `categorize_operation` (переносы 1 868 ₽ returns→fbo и 51 571 ₽ other→compensation), детализация ozon_mp_report, сводка сырых типов в web/app, фильтры по русским именам в ozon_margin_control и promo_bonus_watch;
   - **имя услуги у type 16** (6 операций): влияет только на подстроку детализации «Доставка».
3. delivery_schema у ITEM/NON_ITEM берётся из `raw_ozon_posting`, поэтому этот сбор должен идти раньше адаптера.
4. delivery_charge и return_delivery_charge за период нулевые. Если Ozon снова начнёт их заполнять, аналога в начислении нет: они окажутся внутри услуг доставки.
