# поток: prc — приведение разнобойных имён поставщика к ГРУППЕ из справочника
"""Одно имя поставщика на все отчёты.

Поставщик приходит к нам тремя разными именами: ключом профиля прайса (`kaktus_msk`),
названием юрлица из витрины остатков (`ООО "КОМПАНИЯ ФЕРРЕТ"`) и — там, где карточку
завели руками — вообще ничем. Это один и тот же поставщик, и в отчёте он должен быть
одной строкой: у поставщика за годы менялись ООО и ИП, а группа осталась.

Справочник групп — `invoice_bot/supplier_groups.py`, там же он поддерживается для счетов
и заказов. Здесь только два перевода в него: по ключу профиля (через id контрагентов,
которые профиль и так перечисляет) и по названию юрлица.

Имя, которого в справочнике нет, возвращается как есть: молча схлопывать незнакомого
поставщика в «прочее» нельзя — так пропадёт новый поставщик, которого забыли завести.
"""
from invoice_bot.supplier_groups import ID2GROUP, ID2NAME

_NAME2GROUP = {name.strip().lower(): ID2GROUP[cid] for cid, name in ID2NAME.items()}


def _key2group():
    """Ключ профиля прайса → группа. Ленивая сборка: profiles тянет за собой разбор прайсов."""
    from prices.profiles import PROFILES, IDENTITY
    out = {}
    for src in (PROFILES, IDENTITY):
        for key, prof in src.items():
            for cid in getattr(prof, "supplier_ids", None) or []:
                if cid in ID2GROUP:
                    out[key] = ID2GROUP[cid]
                    break
    return out


_KEY2GROUP = None


def group_of(raw):
    """Группа поставщика по любому из наших имён. None → None, незнакомое имя → само имя."""
    global _KEY2GROUP
    if not raw:
        return None
    if _KEY2GROUP is None:
        _KEY2GROUP = _key2group()
    return _KEY2GROUP.get(raw) or _NAME2GROUP.get(raw.strip().lower()) or raw
