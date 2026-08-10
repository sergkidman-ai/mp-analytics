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

# --- Ozon Real-FBS (rFBS): /v2/returns/rfbs/list, поле state.state ----------------
# Отдельный набор: у rFBS свой словарь статусов, «Едет к вам» здесь MovingToYou (у обычных
# возвратов — MovingToSeller). Разведка 06.08.2026.
OZON_RFBS_PICKUP = {
    "ArrivedAtReturnPlace",        # В пункте выдачи (у Почты России — лежит в отделении)
}
OZON_RFBS_TRANSIT = {
    "MovingToYou",                 # Едет к вам
    "WaitingShipment",             # Ожидает отправки
}
# ReceivedBySeller (Получен), ArrivedForResale и всё, что вне группы delivering
# (деньги/споры/утилизация), — closed: физического действия по ним нет.

# --- Ozon вывоз со склада FBO: /v1/removal/*/list, box_state + return_state -------
# Статусы приходят русскими строками, машинных кодов в отчёте нет.
OZON_REMOVAL_PICKUP = {
    "В пункте выдачи",             # коробка доехала до пункта — забрать
}
OZON_REMOVAL_TRANSIT = {
    "В пути",
    "На СЦ",                       # доехала до сортировочного центра, дальше — в пункт выдачи
    "",                            # коробка ещё не собрана: box_id = 0, статус пуст
}
OZON_REMOVAL_CLOSED = {
    "Получена",                    # забрали
    "Утилизирована",
    "Компенсировано продавцу",     # Ozon заплатил, товар не приедет
}
# Статусы заявки (`return_state`): Создаётся / Собирается на складе / В пути / Можно забирать
# часть / Можно забирать всё / Завершено. Решает статус КОРОБКИ: у заявки «Можно забирать всё»
# часть коробок уже получена, а часть ещё едет.

# --- Яндекс: shipmentStatus -------------------------------------------------------
YANDEX_PICKUP = {
    "READY_FOR_PICKUP",            # готов к выдаче — забрать (у таких заполнен pickupTillDate)
}
YANDEX_TRANSIT = {
    "CREATED",
    "IN_TRANSIT",
    "PREPARED",
    # RECEIVED — «принят в пункте выдачи» логистикой, а НЕ «лежит и ждёт нас». Проверено Сергеем
    # 07.08.2026 на заказах 58716444738 и 59558307072: в ЛК Маркета статус «Готов к отправке»,
    # к выдаче их нет и в пункте их нет. Признак реальной готовности — `pickupTillDate`,
    # он заполнен ровно у READY_FOR_PICKUP и пуст у всех RECEIVED (сверено по базе).
    "RECEIVED",
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

# --- WB: analytics/goods-return, поле `status` + `isStatusActive` ------------------
# Статус приходит русской строкой (машинного кода в отчёте нет), поэтому сверяем по тексту.
# `isStatusActive = 0` перекрывает всё: строка историческая, что бы в статусе ни стояло.
WB_PICKUP = {
    "Готов к выдаче",              # лежит в пункте — забрать
}
WB_TRANSIT = {
    "В пути в пвз",                # едет в пункт, забирать пока нечего
}
# «Выдано» (забрали) и «Отмена по задержке» (не успели, WB забрал себе) приходят с
# isStatusActive = 0 → closed автоматически, перечислять их не нужно.

# Что бот реально показывает. Решение Сергея 02.08.2026: в сводке только то, что ЛЕЖИТ и ждёт
# забора. Убраны «в пути» (Едет к вам / Едет на склад Ozon / Ожидает отправки / Привезёт курьер)
# и «разобраться» (Ищем товар / потерян / готовится к утилизации) — действия по ним всё равно нет.
# Собирать и хранить продолжаем всё: вернуть показ = добавить стадию сюда.
# «Привезёт курьер» (ReturningToSellerByCourier) отложено «возможно, понадобится позже» —
# если возвращать, то отдельной стадией, а не всем блоком transit.
SHOW_STAGES = ("pickup",)

# «Получено нами» — возврат физически у нас на руках. Сверяем по `status_raw` (машинный код там,
# где площадка его даёт; у вывоза FBO и WB кодов нет — русская строка). Отличать от прочих
# «закрыт» обязательно: утилизация, компенсация и отмена — это НЕ получено.
RECEIVED_RAW = {
    "ozon": {"ReceivedBySeller"},
    "ozon_rfbs": {"ReceivedBySeller"},
    "ozon_removal": {"Получена"},
    "wb": {"Выдано"},
    "yandex": {"PICKED"},
}


def is_received(source, status_raw) -> bool:
    return (status_raw or "").strip() in RECEIVED_RAW.get(source, set())

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


def ozon_rfbs_stage(state: str) -> str:
    """Стадия возврата Real-FBS. Всё, кроме доставки, — закрыто (деньги и споры не наша физика)."""
    if state in OZON_RFBS_PICKUP:
        return "pickup"
    if state in OZON_RFBS_TRANSIT:
        return "transit"
    return "closed"


def ozon_removal_stage(box_state: str, return_state: str) -> str:
    """Стадия коробки вывоза со склада FBO. Решает статус коробки, статус заявки — подпорка."""
    box_state = (box_state or "").strip()
    return_state = (return_state or "").strip()
    if box_state in OZON_REMOVAL_PICKUP:
        return "pickup"
    if box_state in OZON_REMOVAL_CLOSED or return_state == "Завершено":
        return "closed"
    if box_state in OZON_REMOVAL_TRANSIT:
        return "transit"
    # живая коробка с неизвестным статусом: молча терять нельзя (см. wb_stage)
    return "attention"


def yandex_stage(shipment_status: str) -> str:
    if shipment_status in YANDEX_PICKUP:
        return "pickup"
    if shipment_status in YANDEX_TRANSIT:
        return "transit"
    if shipment_status in YANDEX_ATTENTION:
        return "attention"
    return "closed"


def wb_stage(status: str, is_active) -> str:
    """Стадия возврата WB. Неактивная строка — история, что бы ни было в статусе."""
    if not is_active:
        return "closed"
    status = (status or "").strip()
    if status in WB_PICKUP:
        return "pickup"
    if status in WB_TRANSIT:
        return "transit"
    # живая строка с неизвестным статусом: молча терять нельзя, но и в пункт по ней
    # не отправляем — пусть всплывёт в «разобраться»
    return "attention"


def is_open(stage: str) -> bool:
    return stage in ("pickup", "transit", "attention")
