# поток: inv
"""Supplier priorities from Dropbox 20261103_101748; strict 20% premium rule."""
from itertools import groupby
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo
from invoice_bot.supplier_groups import ID2GROUP

LEGACY_POLICY_VERSION = 'supplier-priority-20pct-v1'
POLICY_VERSION = 'supplier-priority-20pct-windows-v2'
MSK = ZoneInfo('Europe/Moscow')
_AS_OF = ContextVar('supplier_priority_as_of', default=None)
# Minutes since Monday 00:00; intervals include start and exclude end.
CUSTOM_WINDOWS = {
    'Спринт МСК': ((3*1440+960, 4*1440+960, 15),),
    'Одиссей': ((3*1440+960, 4*1440+960, 20), (4*1440+960, 6*1440+1020, 15)),
    'Изипринт': ((3*1440+960, 4*1440+960, 15), (4*1440+960, 6*1440+1020, 20)),
}
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


def base_supplier_priority(supplier_id):
    if supplier_id in SUPPLIER_PRIORITY:
        return SUPPLIER_PRIORITY[supplier_id]
    return GROUP_PRIORITY.get(SUPPLIER_GROUP.get(supplier_id, ID2GROUP.get(supplier_id)))


def selection_time():
    return _AS_OF.get() or datetime.now(MSK)


def priority_period(as_of):
    with priority_time(as_of) as stamp:
        minute = stamp.weekday()*1440 + stamp.hour*60 + stamp.minute
        return tuple((group, value) for group, windows in sorted(CUSTOM_WINDOWS.items())
                     for start, end, value in windows if start <= minute < end)


def require_current_priority_period():
    if priority_period(selection_time()) != priority_period(datetime.now(MSK)):
        raise RuntimeError('supplier_priority_window_changed_before_write')


@contextmanager
def priority_time(as_of=None):
    stamp = as_of if as_of is not None else selection_time()
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('supplier_priority_timezone_required')
    token = _AS_OF.set(stamp.astimezone(MSK))
    try:
        yield _AS_OF.get()
    finally:
        _AS_OF.reset(token)


def priority_snapshot(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with priority_time():
            return function(*args, **kwargs)
    return wrapped


def supplier_priority(supplier_id, *, as_of=None):
    with priority_time(as_of) as stamp:
        minute = stamp.weekday()*1440 + stamp.hour*60 + stamp.minute
        group = SUPPLIER_GROUP.get(supplier_id, ID2GROUP.get(supplier_id))
        for start, end, value in CUSTOM_WINDOWS.get(group, ()):
            if start <= minute < end:
                return value
    return base_supplier_priority(supplier_id)


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
