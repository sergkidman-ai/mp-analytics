# поток: fin
'''Реестр состава строк Финансы→Баланс Ozon (сверен с ЛК за июль-2026, oz_acc1).

Ключ — код операции/услуги Ozon, значение — (строка отчёта, имя строки в ЛК). Реестр нужен
аудиту `ops/ozon_lines_audit.py`: любой код ВНЕ реестра = новый тип начисления, который молча
уедет в `other`/`delivery` по фолбэку и перекосит отчёт. Правило: сначала свериться с составом
блока в ЛК, потом добавлять правило в `reports/ozon_mp_report.py` и код сюда.'''

RESID_OPS = {   # тип операции → (строка, имя в ЛК)
    'AccrualConsigDefectiveWriteOff': ('compensation', 'Брак по вине Ozon на складе'),
    'AccrualConsigWriteOff': ('compensation', 'Списание товара по вине Ozon'),
    'AccrualInternalClaim': ('compensation', 'Потеря по вине Ozon в логистике'),
    'AccrualWithoutDocs': ('compensation', 'Начисление по спору'),
    'MarketplaceSellerDecompensationItemByTypeDocOperation': ('compensation', 'Декомпенсации и возвращение товаров на склад'),
    'MarketplaceAgencyFeeAggregator3plRFBS': ('delivery', 'Агентское вознаграждение за доставку партнёрами (realFBS)'),
    'OperationCourierArrangement': ('delivery', 'Организация выезда курьера'),
    'OperationCourierPickUpDelivery': ('delivery', 'Доставка курьером Pick-up'),
    'OperationMarketplaceServiceStorage': ('fbo', 'Размещение товаров на складах'),
    'MarketplaceSellerCorrectionOperation': ('other', 'Корректировка начислений'),
    'MarketplaceSellerReexposureDeliveryReturnOperation': ('other', 'Перечисление за доставку от покупателя'),
    'OperationMarketplaceServicePartialCompensationToClient': ('other', 'Перечисления частичных компенсаций покупателям'),
    'MarketplaceServiceEasyReturnRfbs': ('partners', 'Лёгкий возврат (realFBS)'),
    'MarketplaceServiceRedistributionOfDeliveryServicesRFBS': ('partners', 'Услуги доставки партнёрами (realFBS)'),
    'DefectFineCancellation': ('penalty', 'Штраф: отмена'),
    'DefectFineIncomplete': ('penalty', 'Штраф: неполная отгрузка'),
    'DefectFineShipmentDelay': ('penalty', 'Штраф: задержка отгрузки'),
    'DefectFineShipmentDelayRated': ('penalty', 'Штраф: отгрузка в нерекомендованный слот'),
    'DefectFineShipmentDelayRatedCancelled': ('penalty', 'Штраф отменён (нерекомендованный слот)'),
    'DefectFineWrongItem': ('penalty', 'Штраф: не тот товар'),
    'DefectRateCancellation': ('penalty', 'Индекс ошибок: отмена'),
    'DefectRateDetailed': ('penalty', 'Превышение индекса ошибок'),
    'DefectRateShipmentDelay': ('penalty', 'Индекс ошибок: задержка отгрузки'),
    'DefectRateWrongItem': ('penalty', 'Жалобы покупателей / не тот товар'),
    'InsuranceServiceSellerItem': ('penalty', 'Страхование отправления'),
    'OperationMarketplaceFlexiblePaymentSchedule': ('penalty', 'Гибкий график выплат'),
    'OperationMarketplaceServiceEarlyPaymentAccrual': ('penalty', 'Досрочная выплата'),
    'OperationMarketplaceAcceleratedProductReviews': ('promo', 'Ускоренный сбор отзывов'),
    'OperationMarketplaceCostPerClick': ('promo', 'Оплата за клик'),
    'OperationPointsForReviews': ('promo', 'Баллы за отзывы'),
    'OperationPromotionWithCostPerOrder': ('promo', 'Продвижение с оплатой за заказ'),
    'OperationSubscriptionPremium': ('promo', 'Подписка Premium'),
    'OperationSubscriptionPremiumPlus': ('promo', 'Подписка Premium Plus'),
    'OperationSubscriptionPremiumPro': ('promo', 'Подписка Premium Pro'),
}

SERVICES = {   # услуга внутри операции → (строка, имя в ЛК)
    'MarketplaceServiceItemDeliveryToHandoverPlaceOzon': ('delivery', 'Доставка до места выдачи силами Ozon'),
    'MarketplaceServiceItemDirectFlowLogistic': ('delivery', 'Логистика'),
    'MarketplaceServiceItemDropoffPVZ': ('delivery', 'Обработка отправления Drop-off (ПВЗ)'),
    'MarketplaceServiceItemDropoffPickup': ('delivery', 'Обработка отправления Pick-up'),
    'MarketplaceServiceItemDropoffSC': ('delivery', 'Обработка отправления Drop-off'),
    'MarketplaceServiceItemReturnAfterDelivToCustomer': ('delivery', 'возврат после доставки покупателю (0 ₽)'),
    'MarketplaceServiceItemReturnFlowLogistic': ('delivery', 'Обратная логистика'),
    'MarketplaceServiceItemReturnNotDelivToCustomer': ('delivery', 'возврат недоставленного (0 ₽)'),
    'MarketplaceServiceItemReturnPartGoodsCustomer': ('delivery', 'частичный возврат товара (0 ₽)'),
    'MarketplaceServiceProductMovementFromWarehouse': ('fbo', 'Вывоз товара со склада силами Ozon'),
    'MarketplaceServiceSellerReturnsCargoAssortment': ('fbo', 'Подготовка товара к вывозу'),
    'ItemAgentServiceStarsMembership': ('partners', 'Звёздные товары'),
    'MarketplaceRedistributionOfAcquiringOperation': ('partners', 'Эквайринг'),
    'MarketplaceServiceItemPackageRedistribution': ('partners', 'Упаковка товара партнёрами'),
    'MarketplaceServiceItemRedistributionDropOffApvz': ('partners', 'Обработка отправления Drop-off партнёрами'),
    'MarketplaceServiceItemRedistributionLastMileCourier': ('partners', 'Доставка до места выдачи партнёрами'),
    'MarketplaceServiceItemRedistributionLastMilePVZ': ('partners', 'Доставка до места выдачи партнёрами (ПВЗ)'),
    'MarketplaceServiceItemRedistributionReturnsPVZ': ('partners', 'Обработка возвратов, отмен и невыкупов партнёрами'),
    'MarketplaceServiceItemTemporaryStorageRedistribution': ('partners', 'Временное размещение товара партнёрами'),
    'MarketplaceServiceItemDisposalDetailed': ('penalty', 'Утилизация товара'),
    'MarketplaceServiceItemPackageMaterialsProvision': ('penalty', 'Обеспечение материалами для упаковки'),
    'MarketplaceServiceVolumeWeightCharacsProcessing': ('penalty', 'Обработка характеристик (объёмный вес)'),
    'MarketplaceServiceItemElectronicServicesPremiumCashbackIndividualPoints': ('promo', 'Баллы Premium (индивидуальные)'),
    'PremiumMembershipCommission': ('promo', 'Подписка Premium Pro (процент)'),
}

# Эталон ЛК: Финансы→Баланс, oz_acc1, июль-2026 (скриншоты 2026-08-12).
LK_REF = {
    ('oz_acc1', 2026, 7): {'sales': 10558895, 'returns': 263916, 'commission': 4172835,
        'delivery': 303430, 'partners': 320759, 'fbo': 13434, 'promo': 1071077,
        'penalty': 230534, 'compensation': 25591, 'other': 30614},
}
