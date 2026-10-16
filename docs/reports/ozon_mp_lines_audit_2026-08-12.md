# Аудит строк Финансы→Баланс Ozon, 2026-01…2026-07

Прогон 2026-08-12, `ops/ozon_lines_audit.py`.

## Новые коды (нет в реестре — уехали в фолбэк)

нет

## Сменили строку относительно реестра
нет

## Сверка с ЛК (эталонные месяцы)
- oz_acc1 2026-07 `delivery`: ЛК 303,430 / наше 303,974 — дельта +544
- oz_acc1 2026-07 `partners`: ЛК 320,759 / наше 320,827 — дельта +68

## Состав строк (сумма за период, ₽)

### delivery

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceServiceItemDirectFlowLogistic` | Логистика | 3,223,638.73 | 11264 |
| `OperationCourierPickUpDelivery` | Доставка курьером Pick-up | 187,175.50 | 154 |
| `MarketplaceServiceItemDropoffPickup` | Обработка отправления Pick-up | 131,878.89 | 6642 |
| `MarketplaceServiceItemReturnFlowLogistic` | Обратная логистика | 130,426.11 | 723 |
| `OperationCourierArrangement` | Организация выезда курьера | 106,330.00 | 153 |
| `MarketplaceServiceItemDeliveryToHandoverPlaceOzon` | Доставка до места выдачи силами Ozon | 41,700.00 | 1235 |
| `MarketplaceServiceItemDropoffPVZ` | Обработка отправления Drop-off (ПВЗ) | 34,810.00 | 3418 |
| `MarketplaceAgencyFeeAggregator3plRFBS` | Агентское вознаграждение за доставку партнёрами (realFBS) | 18,750.00 | 938 |
| `MarketplaceServiceItemDropoffSC` | Обработка отправления Drop-off | 320.00 | 25 |
| `MarketplaceServiceItemReturnNotDelivToCustomer` | возврат недоставленного (0 ₽) | 0.00 | 220 |
| `MarketplaceServiceItemReturnAfterDelivToCustomer` | возврат после доставки покупателю (0 ₽) | 0.00 | 117 |
| `MarketplaceServiceItemReturnPartGoodsCustomer` | частичный возврат товара (0 ₽) | 0.00 | 15 |

### partners

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceServiceRedistributionOfDeliveryServicesRFBS` | Услуги доставки партнёрами (realFBS) | 693,023.90 | 938 |
| `ItemAgentServiceStarsMembership` | Звёздные товары | 517,774.67 | 5258 |
| `MarketplaceRedistributionOfAcquiringOperation` | Эквайринг | 507,618.27 | 10947 |
| `MarketplaceServiceItemRedistributionLastMileCourier` | Доставка до места выдачи партнёрами | 109,376.41 | 8896 |
| `MarketplaceServiceItemRedistributionDropOffApvz` | Обработка отправления Drop-off партнёрами | 79,359.56 | 4039 |
| `MarketplaceServiceItemTemporaryStorageRedistribution` | Временное размещение товара партнёрами | 7,320.00 | 69 |
| `MarketplaceServiceItemRedistributionReturnsPVZ` | Обработка возвратов, отмен и невыкупов партнёрами | 5,175.00 | 345 |
| `MarketplaceServiceEasyReturnRfbs` | Лёгкий возврат (realFBS) | 1,908.50 | 12 |
| `MarketplaceServiceItemPackageRedistribution` | Упаковка товара партнёрами | 920.00 | 92 |
| `MarketplaceServiceItemRedistributionLastMilePVZ` | Доставка до места выдачи партнёрами (ПВЗ) | -250.00 | 1 |

### fbo

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `OperationMarketplaceServiceStorage` | Размещение товаров на складах | 46,628.46 | 384 |
| `MarketplaceServiceSellerReturnsCargoAssortment` | Подготовка товара к вывозу | 17,800.00 | 221 |
| `MarketplaceServiceProductMovementFromWarehouse` | Вывоз товара со склада силами Ozon | 14,458.20 | 221 |

### promo

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `OperationPromotionWithCostPerOrder` | Продвижение с оплатой за заказ | 2,304,254.12 | 266 |
| `OperationMarketplaceCostPerClick` | Оплата за клик | 2,188,628.75 | 2532 |
| `OperationMarketplaceAcceleratedProductReviews` | Ускоренный сбор отзывов | 287,322.20 | 216 |
| `PremiumMembershipCommission` | Подписка Premium Pro (процент) | 287,035.96 | 1301 |
| `OperationSubscriptionPremiumPlus` | Подписка Premium Plus | 274,890.00 | 11 |
| `OperationPointsForReviews` | Баллы за отзывы | 148,698.48 | 142 |
| `MarketplaceServiceItemElectronicServicesPremiumCashbackIndividualPoints` | Баллы Premium (индивидуальные) | 57,859.83 | 5156 |
| `OperationSubscriptionPremiumPro` | Подписка Premium Pro | 49,980.00 | 2 |
| `OperationSubscriptionPremium` | Подписка Premium | 19,980.00 | 2 |

### penalty

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `DefectFineShipmentDelayRated` | Штраф: отгрузка в нерекомендованный слот | 510,192.75 | 3055 |
| `OperationMarketplaceServiceEarlyPaymentAccrual` | Досрочная выплата | 138,129.19 | 10 |
| `DefectFineShipmentDelay` | Штраф: задержка отгрузки | 90,042.12 | 346 |
| `DefectRateDetailed` | Превышение индекса ошибок | 80,078.84 | 579 |
| `DefectRateShipmentDelay` | Индекс ошибок: задержка отгрузки | 39,434.48 | 117 |
| `OperationMarketplaceFlexiblePaymentSchedule` | Гибкий график выплат | 27,090.91 | 2 |
| `DefectFineCancellation` | Штраф: отмена | 22,838.55 | 46 |
| `DefectRateCancellation` | Индекс ошибок: отмена | 13,574.87 | 42 |
| `DefectFineShipmentDelayRatedCancelled` | Штраф отменён (нерекомендованный слот) | -12,338.89 | 63 |
| `MarketplaceServiceVolumeWeightCharacsProcessing` | Обработка характеристик (объёмный вес) | 10,950.00 | 33 |
| `DefectFineWrongItem` | Штраф: не тот товар | 5,624.80 | 17 |
| `DefectRateWrongItem` | Жалобы покупателей / не тот товар | 1,272.34 | 8 |
| `MarketplaceServiceItemDisposalDetailed` | Утилизация товара | 1,220.55 | 14 |
| `InsuranceServiceSellerItem` | Страхование отправления | 1,129.26 | 30 |
| `DefectFineIncomplete` | Штраф: неполная отгрузка | 776.34 | 1 |
| `MarketplaceServiceItemPackageMaterialsProvision` | Обеспечение материалами для упаковки | 460.00 | 92 |

### compensation

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `AccrualInternalClaim` | Потеря по вине Ozon в логистике | -49,572.41 | 9 |
| `AccrualConsigDefectiveWriteOff` | Брак по вине Ozon на складе | -24,407.41 | 4 |
| `AccrualWithoutDocs` | Начисление по спору | -14,369.96 | 3 |
| `AccrualConsigWriteOff` | Списание товара по вине Ozon | -10,526.28 | 2 |
| `MarketplaceSellerDecompensationItemByTypeDocOperation` | Декомпенсации и возвращение товаров на склад | 4,478.56 | 1 |

### other

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceSellerCorrectionOperation` | Корректировка начислений | -331,620.75 | 3 |
| `MarketplaceSellerReexposureDeliveryReturnOperation` | Перечисление за доставку от покупателя | -291,731.00 | 840 |
| `OperationMarketplaceServicePartialCompensationToClient` | Перечисления частичных компенсаций покупателям | 1,738.63 | 1 |
