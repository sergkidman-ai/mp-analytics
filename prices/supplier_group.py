# поток: prc
# -*- coding: utf-8 -*-
"""Группа юрлиц поставщика для прайсов и оприходований.

У поставщика за годы сменилось несколько ООО/ИП, и товары в МС остались висеть на разных
(в том числе закрытых) юрлицах одной группы. Поэтому «свой товар» — это не контрагент из
профиля прайса, а ЛЮБОЕ юрлицо той же группы. Таблица групп одна на проект и живёт в
`invoice_bot/supplier_groups.py` (её же использует разбор счетов и УПД) — здесь только
переход «профиль прайса → группа», без второго списка юрлиц.

Группу берём по контрагенту профиля, а не по имени поставщика: имена в профиле и в МС
расходятся («Кактус» — это «ООО КОМПАНИЯ ФЕРРЕТ»), а id однозначен.
"""
from invoice_bot.supplier_groups import ID2GROUP, ID2NAME, group_ids


def group_of(profile):
    """Имя группы поставщика по профилю прайса. None — группы в таблице нет."""
    for sid in profile.supplier_ids or ():
        group = ID2GROUP.get(sid)
        if group:
            return group
    return None


def own_ids(profile):
    """id всех юрлиц поставщика: группа целиком плюс то, что явно указано в профиле."""
    group = group_of(profile)
    return (group_ids(group) if group else set()) | set(profile.supplier_ids or ())


def name_of(counterparty_id):
    """Имя контрагента для отчётов; чего нет в таблице групп — отдаём id."""
    return ID2NAME.get(counterparty_id, counterparty_id)
