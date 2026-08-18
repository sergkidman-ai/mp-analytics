# Аудит строк Финансы→Баланс Ozon, 2025-02…2025-12

Прогон 2026-08-18, `ops/ozon_lines_audit.py`.

## Новые коды (нет в реестре — ушли в «Неклассифицировано»)

- **MarketplaceCorrectionPointOperation** (операция) → сейчас падает в `unclassified`
- **MarketplaceSaleReviewsOperation** (операция) → сейчас падает в `unclassified`
- **MarketplaceSellerCompensationOperation** (операция) → сейчас падает в `unclassified`
- **MarketplaceServiceItemCrossdocking** (операция) → сейчас падает в `unclassified`
- **MarketplaceServiceItemServiceFeeRFBS** (операция) → сейчас падает в `unclassified`
- **MarketplaceShipmentMovingOperation** (операция) → сейчас падает в `unclassified`
- **OperationElectronicServiceStencil** (операция) → сейчас падает в `unclassified`
- **OperationElectronicServicesPromotionInSearch** (операция) → сейчас падает в `unclassified`
- **OperationGettingToTheTop** (операция) → сейчас падает в `unclassified`
- **OperationMarketPlaceItemPinReview** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceServicePreparingToReturn** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceServiceSupplyInboundCargoShortage** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceServiceSupplyInboundCargoSurplus** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceServiceSupplyInboundCrossZoneAcceptance** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceSupplyExpirationDateProcessing** (операция) → сейчас падает в `unclassified`
- **OperationMarketplaceWithHoldingForUndeliverableGoods** (операция) → сейчас падает в `unclassified`
- **OperationModerationProhibitedContent** (операция) → сейчас падает в `unclassified`
- **OperationOtherElectronicServices** (операция) → сейчас падает в `unclassified`
- **MarketplaceServiceItemDelivToCustomer** (услуга) → сейчас падает в `unclassified`
- **MarketplaceServiceItemDirectFlowLogisticVDC** (услуга) → сейчас падает в `unclassified`
- **MarketplaceServiceItemTemporaryStorage** (услуга) → сейчас падает в `unclassified`
- **PremiumMembershipCommissionCancelled** (услуга) → сейчас падает в `unclassified`

## Сменили строку относительно реестра
нет

## Сверка с ЛК (эталонные месяцы)
расхождений нет

## Состав строк (сумма за период, ₽)

### delivery

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceServiceItemDirectFlowLogistic` | Логистика | 3,995,297.34 | 14370 |
| `OperationCourierPickUpDelivery` | Доставка курьером Pick-up | 149,197.30 | 171 |
| `MarketplaceServiceItemReturnFlowLogistic` | Обратная логистика | 125,985.58 | 725 |
| `MarketplaceServiceItemDropoffPickup` | Обработка отправления Pick-up | 125,280.00 | 6266 |
| `OperationCourierArrangement` | Организация выезда курьера | 119,700.00 | 171 |
| `MarketplaceServiceItemDropoffPVZ` | Обработка отправления Drop-off (ПВЗ) | 43,205.00 | 3201 |
| `MarketplaceAgencyFeeAggregator3plRFBS` | Агентское вознаграждение за доставку партнёрами (realFBS) | 23,265.00 | 1551 |
| `MarketplaceServiceItemDropoffSC` | Обработка отправления Drop-off | 390.00 | 22 |
| `MarketplaceServiceItemReturnNotDelivToCustomer` | возврат недоставленного (0 ₽) | 0.00 | 249 |
| `MarketplaceServiceItemReturnAfterDelivToCustomer` | возврат после доставки покупателю (0 ₽) | 0.00 | 381 |
| `MarketplaceServiceItemReturnPartGoodsCustomer` | частичный возврат товара (0 ₽) | 0.00 | 95 |

### partners

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceServiceRedistributionOfDeliveryServicesRFBS` | Услуги доставки партнёрами (realFBS) | 912,041.98 | 1551 |
| `MarketplaceRedistributionOfAcquiringOperation` | Эквайринг | 576,759.64 | 15207 |
| `MarketplaceServiceItemRedistributionLastMileCourier` | Доставка до места выдачи партнёрами | 115,724.32 | 7388 |
| `MarketplaceServiceItemRedistributionDropOffApvz` | Обработка отправления Drop-off партнёрами | 52,765.00 | 3201 |
| `MarketplaceServiceItemRedistributionLastMilePVZ` | Доставка до места выдачи партнёрами (ПВЗ) | 36,714.13 | 342 |
| `MarketplaceServiceItemRedistributionReturnsPVZ` | Обработка возвратов, отмен и невыкупов партнёрами | 7,650.00 | 510 |
| `MarketplaceServiceEasyReturnRfbs` | Лёгкий возврат (realFBS) | 2,190.00 | 15 |
| `MarketplaceServiceItemTemporaryStorageRedistribution` | Временное размещение товара партнёрами | 198.00 | 4 |

### fbo

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `OperationMarketplaceServiceStorage` | Размещение товаров на складах | 97,443.07 | 333 |
| `MarketplaceServiceSellerReturnsCargoAssortment` | Подготовка товара к вывозу | 13,818.00 | 168 |
| `MarketplaceServiceProductMovementFromWarehouse` | Вывоз товара со склада силами Ozon | 11,558.40 | 191 |

### promo

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `OperationPromotionWithCostPerOrder` | Продвижение с оплатой за заказ | 1,881,662.33 | 189 |
| `OperationMarketplaceCostPerClick` | Оплата за клик | 1,329,453.98 | 780 |
| `OperationPointsForReviews` | Баллы за отзывы | 393,883.20 | 297 |
| `OperationSubscriptionPremiumPlus` | Подписка Premium Plus | 254,736.77 | 12 |
| `PremiumMembershipCommission` | Подписка Premium Pro (процент) | 134,601.85 | 1587 |
| `MarketplaceServiceItemElectronicServicesPremiumCashbackIndividualPoints` | Баллы Premium (индивидуальные) | 100,755.81 | 9401 |
| `OperationSubscriptionPremiumPro` | Подписка Premium Pro | 24,990.00 | 1 |

### penalty

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `DefectRateShipmentDelay` | Индекс ошибок: задержка отгрузки | 322,484.53 | 871 |
| `DefectRateDetailed` | Превышение индекса ошибок | 267,350.42 | 2516 |
| `DefectRateCancellation` | Индекс ошибок: отмена | 37,245.50 | 175 |
| `OperationMarketplaceServiceEarlyPaymentAccrual` | Досрочная выплата | 19,256.31 | 1 |
| `MarketplaceServiceItemDisposalDetailed` | Утилизация товара | 1,217.73 | 10 |
| `DefectRateWrongItem` | Жалобы покупателей / не тот товар | 0.00 | 2 |

### unclassified

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `OperationGettingToTheTop` | — | 955,694.34 | 1074 |
| `OperationElectronicServicesPromotionInSearch` | — | 952,162.09 | 108 |
| `MarketplaceServiceItemDelivToCustomer` | — | 781,102.53 | 5568 |
| `OperationElectronicServiceStencil` | — | 470,693.93 | 209 |
| `OperationMarketPlaceItemPinReview` | — | 393,400.00 | 7 |
| `MarketplaceShipmentMovingOperation` | — | 187,023.14 | 74 |
| `MarketplaceServiceItemDirectFlowLogisticVDC` | — | 30,407.82 | 130 |
| `OperationOtherElectronicServices` | — | 25,882.61 | 3 |
| `MarketplaceSaleReviewsOperation` | — | 21,168.00 | 27 |
| `MarketplaceServiceItemCrossdocking` | — | 15,950.00 | 39 |
| `OperationModerationProhibitedContent` | — | -15,000.00 | 15 |
| `OperationMarketplaceWithHoldingForUndeliverableGoods` | — | 2,532.20 | 1 |
| `MarketplaceCorrectionPointOperation` | — | -1,368.70 | 5 |
| `OperationMarketplaceServiceSupplyInboundCrossZoneAcceptance` | — | 1,305.00 | 2 |
| `OperationMarketplaceServicePreparingToReturn` | — | 1,250.00 | 19 |
| `MarketplaceServiceItemTemporaryStorage` | — | 1,188.00 | 15 |
| `OperationMarketplaceServiceSupplyInboundCargoSurplus` | — | 865.00 | 4 |
| `OperationMarketplaceServiceSupplyInboundCargoShortage` | — | 865.00 | 4 |
| `MarketplaceServiceItemServiceFeeRFBS` | — | 660.00 | 44 |
| `PremiumMembershipCommissionCancelled` | — | -568.23 | 1 |
| `OperationMarketplaceSupplyExpirationDateProcessing` | — | 272.00 | 25 |
| `MarketplaceSellerCompensationOperation` | — | -93.98 | 1 |

### compensation

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `AccrualInternalClaim` | Потеря по вине Ozon в логистике | -60,964.75 | 7 |
| `AccrualWithoutDocs` | Начисление по спору | -10,529.28 | 3 |
| `MarketplaceSellerDecompensationItemByTypeDocOperation` | Декомпенсации и возвращение товаров на склад | 10,361.39 | 1 |

### other

| код | имя в ЛК | сумма | строк |
|---|---|---:|---:|
| `MarketplaceSellerReexposureDeliveryReturnOperation` | Перечисление за доставку от покупателя | -301,504.00 | 1302 |
| `MarketplaceSellerCorrectionOperation` | Корректировка начислений | -8,783.92 | 19 |
| `OperationMarketplaceServicePartialCompensationToClient` | Перечисления частичных компенсаций покупателям | 2,178.00 | 1 |
