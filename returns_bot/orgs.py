# поток: ret
"""Юрлицо → аккаунты площадок. Сводка шлётся отдельным сообщением на каждое юрлицо.

Яндекс: один ключ `ya_acc1` и три кампании («Москва наш склад», «Москва Звездный»,
«Москва экспресс») — все под Цифровым. Появится Яндекс у Дисквэра — заводить отдельный
аккаунт `ya_acc2` в источнике, а не разносить кампании руками.
"""

DIGITAL = "Цифровой"
DISQUARE = "Дисквэр"

ORG_BY_ACCOUNT = {
    ("ozon", "oz_acc1"): DIGITAL,
    ("ozon", "oz_acc2"): DISQUARE,
    ("yandex", "ya_acc1"): DIGITAL,
    ("wb", "wb_acc1"): DIGITAL,
    ("wb", "wb_acc2"): DISQUARE,
}

ORDER = [DIGITAL, DISQUARE]


def of(platform, account):
    """Юрлицо аккаунта. Неизвестный аккаунт не теряем — показываем под своим именем."""
    return ORG_BY_ACCOUNT.get((platform, account), account)


def accounts(org):
    return [k for k, v in ORG_BY_ACCOUNT.items() if v == org]


def order(present):
    """Известные юрлица в фиксированном порядке, неизвестные — следом по алфавиту."""
    known = [o for o in ORDER if o in present]
    return known + sorted(set(present) - set(known))
