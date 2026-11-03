# поток: inv
"""Supplier priorities from Dropbox 20261103_101748; strict 20% premium rule."""
from itertools import groupby
from invoice_bot.supplier_groups import ID2GROUP

POLICY_VERSION = 'supplier-priority-20pct-v1'
GROUP_PRIORITY = {
    'Жухай': 10, 'Одиссей': 20, 'Суперфайн': 20, 'Сакура': 20,
    'Изипринт': 20, 'Спринт МСК': 20, 'Колортек': 30, 'Профилайн': 30,
    'Булат': 30, 'Кактус': 30, 'Спринт': 40, 'НВ-Принт': 40,
    'Юнитон': 40, 'Блоссом': 40, 'ВТТ': 40,
}
SUPPLIER_GROUP = {
    'c16b41ef-3428-11ed-0a80-0e48002629ce': 'Спринт',
    '59ae3427-7e0e-11ee-0a80-04960008d1f8': 'Спринт',
    'dd6f9218-ec82-11e6-7a69-97110027a1f0': 'Жухай',
}
SUPPLIER_PRIORITY = {
    '61d01266-2fe1-11f0-0a80-0f7f000bf967': 15,  # Одиссей WB
    'daffd8b7-308f-11f0-0a80-05570004fd14': 15,  # ТОНЕРОПТТОРГ T2
    # КВК ТРЕЙД OEM is 20, same as the Superfine group.
}


def supplier_priority(supplier_id):
    if supplier_id in SUPPLIER_PRIORITY:
        return SUPPLIER_PRIORITY[supplier_id]
    return GROUP_PRIORITY.get(SUPPLIER_GROUP.get(supplier_id, ID2GROUP.get(supplier_id)))


def choose_with_premium(items, *, priority, price, identity):
    """Cheapest in each priority bucket; >20% above a cheaper offer loses priority.

    Unknown prices cannot establish a premium. Such items may only be supplied
    from verified own stock; callers exclude them from purchase alternatives.
    Works for individual offers and for complete, equally feasible kit plans.
    """
    ordered = sorted(items, key=lambda x: (priority(x),
        price(x) is None, price(x) if price(x) is not None else 0, identity(x)))
    if not ordered:
        raise ValueError('no_selection_candidates')
    buckets = [next(group) for _, group in groupby(ordered, key=priority)]
    best = buckets[0]
    for alternative in buckets[1:]:
        old, new = price(best), price(alternative)
        if old is not None and new is not None and old * 100 > new * 120:
            best = alternative
    return best
