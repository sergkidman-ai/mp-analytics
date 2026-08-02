# поток: ret
"""Правила «висит и ждёт забора».

Единственное место, где перечислены статусы площадок. Правка правил — только здесь.

Стадии:
    pickup    — лежит в точке выдачи/у нас на руках нет: ЗАБРАТЬ;
    transit   — едет к нам, забирать пока нечего, но скоро приедет;
    attention — потерян/утилизируется/просрочен: разобраться;
    closed    — история, в сводку не идёт.

Основание (разведка 02.08.2026, docs/reports/returns_api_recon.md): у Ozon статус приходит
стабильным `visual.status.sys_name` (русская подпись — только для показа), у Яндекса —
`shipmentStatus`. Терминальные состояния дополнительно подтверждаются непустым
`logistic.final_moment` у Ozon.
"""

# --- Ozon: visual.status.sys_name -------------------------------------------------
OZON_PICKUP = {
    "ArrivedAtReturnPlace",        # В пункте выдачи — лежит, забрать
}
OZON_TRANSIT = {
    "MovingToSeller",              # Едет к вам
    "ReturningToSellerByCourier",  # Привезет курьер
    "WaitingShipment",             # Ожидает отправки
    "MovingToOzon",                # Едет на склад Ozon
}
OZON_ATTENTION = {
    "PotentiallyLost",             # Ищем товар — судьба ещё решается
}
# Всё остальное — closed. Сюда же сознательно отнесены Utilized («Утилизирован») и
# WriteOff («Списали товар»): товара уже нет, забирать нечего, в ежедневный список действий
# они попадать не должны (у acc1 это 34 старые строки шума).

# --- Яндекс: shipmentStatus -------------------------------------------------------
YANDEX_PICKUP = {
    "READY_FOR_PICKUP",            # готов к выдаче — забрать (у таких заполнен pickupTillDate)
    "RECEIVED",                    # принят точкой выдачи
}
YANDEX_TRANSIT = {
    "CREATED",
    "IN_TRANSIT",
    "PREPARED",
}
YANDEX_ATTENTION = {
    "LOST",                        # потерян — спорить с площадкой
    "PREPARED_FOR_UTILIZATION",    # вот-вот утилизируют — последний шанс забрать
}
# PICKED (забран нами), CANCELLED, UTILIZED, EXPIRED, RECEIVED_FOR_EXPROPRIATION — closed:
# товар уже недоступен, действие невозможно.

YANDEX_STATUS_RU = {
    "CREATED": "создан",
    "RECEIVED": "принят в пункте выдачи",
    "IN_TRANSIT": "в пути",
    "READY_FOR_PICKUP": "готов к выдаче",
    "PICKED": "забран",
    "CANCELLED": "отменён",
    "LOST": "потерян",
    "EXPIRED": "просрочен",
    "PREPARED": "подготовлен",
    "PREPARED_FOR_UTILIZATION": "готовится к утилизации",
    "RECEIVED_FOR_EXPROPRIATION": "изъят",
    "UTILIZED": "утилизирован",
}

# Что бот реально показывает. Решение Сергея 02.08.2026: в сводке только то, что ЛЕЖИТ и ждёт
# забора. Убраны «в пути» (Едет к вам / Едет на склад Ozon / Ожидает отправки / Привезёт курьер)
# и «разобраться» (Ищем товар / потерян / готовится к утилизации) — действия по ним всё равно нет.
# Собирать и хранить продолжаем всё: вернуть показ = добавить стадию сюда.
# «Привезёт курьер» (ReturningToSellerByCourier) отложено «возможно, понадобится позже» —
# если возвращать, то отдельной стадией, а не всем блоком transit.
SHOW_STAGES = ("pickup",)

STAGE_ORDER = ["pickup", "attention", "transit", "closed"]
STAGE_TITLE = {
    "pickup": "ЗАБРАТЬ",
    "attention": "РАЗОБРАТЬСЯ",
    "transit": "В ПУТИ",
    "closed": "закрыт",
}


def ozon_stage(sys_name: str, final_moment=None) -> str:
    """Стадия возврата Ozon. final_moment непустой = процесс завершён."""
    if sys_name in OZON_PICKUP:
        return "pickup"
    if sys_name in OZON_TRANSIT:
        # процесс закрыт площадкой — движение уже неактуально
        return "transit" if not final_moment else "closed"
    if sys_name in OZON_ATTENTION:
        return "attention"
    return "closed"


def yandex_stage(shipment_status: str) -> str:
    if shipment_status in YANDEX_PICKUP:
        return "pickup"
    if shipment_status in YANDEX_TRANSIT:
        return "transit"
    if shipment_status in YANDEX_ATTENTION:
        return "attention"
    return "closed"


def is_open(stage: str) -> bool:
    return stage in ("pickup", "transit", "attention")
