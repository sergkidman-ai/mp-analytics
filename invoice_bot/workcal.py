"""Рабочий календарь РФ для План. даты приёмки.
5-дневка: рабочий день = isdayoff.ru==0 (учитывает праздники, выходные, переносы).
6-дневка: плюс субботы, кроме субботы-праздника (May 9 и т.п.).
Праздник = выходной для всех. Кэш в workcal_cache.json, фолбэк без сети."""
import json, os, urllib.request
from datetime import date, timedelta

_CACHE_F=os.path.join(os.path.dirname(__file__),"workcal_cache.json")
try: _CACHE=json.load(open(_CACHE_F))
except Exception: _CACHE={}

# фикс. федеральные праздники (месяц,день) — для фолбэка и для субботы-6дневки
FIXED={(1,1),(1,2),(1,3),(1,4),(1,5),(1,6),(1,7),(1,8),(2,23),(3,8),(5,1),(5,9),(6,12),(11,4)}

def _fallback(d): return 1 if (d.weekday()>=5 or (d.month,d.day) in FIXED) else 0

def isdayoff(d):
    k=d.strftime("%Y%m%d")
    if k in _CACHE: return _CACHE[k]
    try:
        with urllib.request.urlopen(f"https://isdayoff.ru/{k}", timeout=8) as r:
            v=r.read().decode().strip()
        val=int(v[0]) if v and v[0] in "01" else _fallback(d)
    except Exception:
        val=_fallback(d)
    _CACHE[k]=val
    try: json.dump(_CACHE, open(_CACHE_F,"w"))
    except Exception: pass
    return val

def is_holiday(d):  # именно праздник (не просто выходной)
    return (d.month,d.day) in FIXED

def is_working(d, six=False):
    if isdayoff(d)==0: return True                      # офиц. рабочий (вкл. перенос-субботы)
    # 6-дневка: работает субботу ТОЛЬКО если это обычная рабочая суббота —
    # т.е. не праздник и предшествующая пятница рабочая (в праздничный блок 6-дневка закрыта).
    if six and d.weekday()==5 and not is_holiday(d) and isdayoff(d - timedelta(days=1))==0:
        return True
    return False

def plan_date(inv, six=False):
    d=inv+timedelta(days=1)
    while not is_working(d, six): d+=timedelta(days=1)
    return d

if __name__=="__main__":
    wd=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    tests=[
        ("Тонеропттторг Пт 6дн", date(2026,5,29), True),
        ("Одиссей Пт 5дн", date(2026,7,17), False),
        ("перед НГ 5дн", date(2025,12,31), False),
        ("перед НГ 6дн", date(2025,12,31), True),
        ("перед 8 марта Пт 5дн", date(2026,3,6), False),
        ("перед 8 марта Пт 6дн(Сб 7е раб?)", date(2026,3,6), True),
        ("перед Днём Победы 5дн", date(2026,5,8), False),
        ("перед Днём Победы 6дн (Сб 9е=праздник)", date(2026,5,8), True),
    ]
    for name,d,six in tests:
        p=plan_date(d,six)
        print(f"{name}: счёт {d}({wd[d.weekday()]}) → приёмка {p}({wd[p.weekday()]})")
