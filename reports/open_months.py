# поток: fin
"""Choose an open MP reporting month without closing or recomputing history."""
import datetime as dt
import re
from zoneinfo import ZoneInfo


def select_open_month(period,closed,*,today=None):
    if not closed or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])(?:-01)?',period or ''):
        return None
    key=period[:7]
    current=(today or dt.datetime.now(ZoneInfo('Europe/Moscow')).date()).strftime('%Y-%m')
    if key<=max(closed) or key>current:
        return None
    return tuple(map(int,key.split('-')))
