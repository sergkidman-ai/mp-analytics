# поток: inv
"""Supplier dispatch lead times; calendar rules explicitly agreed by the operator."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from dateutil.parser import isoparse
from invoice_bot.supplier_groups import ID2GROUP
from ops.ms_supplier_priorities import SUPPLIER_GROUP

MSK = ZoneInfo('Europe/Moscow')
POLICY_VERSION = 'supplier-workdays-after15-v1'
SATURDAY_GROUPS = frozenset({'Спринт', 'Изипринт'})
TWO_DAY_GROUPS = frozenset({'Блоссом', 'Колортек'})
# Additional days off explicitly specified for the operational calendar.
DAYS_OFF = frozenset({date(2026, 11, 4)})


def supplier_group(supplier_id):
    return SUPPLIER_GROUP.get(supplier_id, ID2GROUP.get(supplier_id))


def local_created(value):
    stamp = isoparse(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime):
        raise ValueError('order_creation_time_required')
    return (stamp.replace(tzinfo=MSK) if stamp.tzinfo is None else stamp.astimezone(MSK))


def working_day(day, group):
    return day not in DAYS_OFF and day.weekday() < (6 if group in SATURDAY_GROUPS else 5)


def add_workdays(day, count, group):
    for _ in range(count):
        day += timedelta(days=1)
        while not working_day(day, group):
            day += timedelta(days=1)
    return day


def shortage_day(created_at, supplier_ids):
    created = local_created(created_at)
    extra = int(created.time().replace(tzinfo=None) > time(15))
    groups = {supplier_group(uid) for uid in supplier_ids} or {None}
    return max(add_workdays(created.date(), (2 if group in TWO_DAY_GROUPS else 1) + extra, group)
               for group in groups)
