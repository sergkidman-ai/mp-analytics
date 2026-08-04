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

def _save():
    try: json.dump(_CACHE, open(_CACHE_F,"w"))
    except Exception: pass

def _fetch(d):
    """Свежий ответ сервиса: 0/1, либо None — сервис ответил не по делу.
    Коды ошибок (100 «дата не разобрана», 101 «нет данных», 199) приходят с HTTP 4xx,
    но тело проверяем тоже: старый разбор брал ПЕРВЫЙ символ, и «100» читалось как выходной."""
    try:
        with urllib.request.urlopen(f"https://isdayoff.ru/{d:%Y%m%d}", timeout=8) as r:
            v=r.read().decode().strip()
    except Exception:
        return None
    return int(v) if v in ("0","1") else None

def isdayoff(d):
    k=d.strftime("%Y%m%d")
    if k in _CACHE: return _CACHE[k]
    v=_fetch(d)
    if v is None:
        return _fallback(d)      # догадку НЕ кэшируем: замёрзнет навсегда. Даты за горизонтом
                                 # сервиса отдают 404 → перенос-суббота осталась бы «выходной»
                                 # и после появления данных.
    _CACHE[k]=v; _save()
    return v

def is_holiday(d):  # именно праздник (не просто выходной)
    return (d.month,d.day) in FIXED

def _suspicious(d):
    """Будний день объявлен выходным вдали от праздников — это не перенос, а сбой ответа:
    03.08.2026 дал план приёмки 05.08 вместо 04.08 (заказ КТ-000117, Блоссом).
    Настоящие переносы всегда примыкают к праздничному блоку."""
    if d.weekday()>=5: return False
    return not any(is_holiday(d+timedelta(days=o)) for o in range(-3,4))

def is_working(d, six=False):
    off=isdayoff(d)
    if off==1 and _suspicious(d):                       # перепроверяем мимо кэша и чиним кэш
        fresh=_fetch(d)
        if fresh is not None and fresh!=off:
            _CACHE[d.strftime("%Y%m%d")]=fresh; _save(); off=fresh
    if off==0: return True                              # офиц. рабочий (вкл. перенос-субботы)
    # 6-дневка: работает субботу ТОЛЬКО если это обычная рабочая суббота —
    # т.е. не праздник и предшествующая пятница рабочая (в праздничный блок 6-дневка закрыта).
    if six and d.weekday()==5 and not is_holiday(d) and isdayoff(d - timedelta(days=1))==0:
        return True
    return False

def plan_date(inv, six=False, skip=0):
    """Плановая дата приёмки от даты счёта.
    skip=0 — ближайший рабочий день (счёт Пн 03.08 → Вт 04.08).
    skip=1 — «через 1 рабочий день»: один полный рабочий день пропускаем
    (счёт Пн 03.08 → Ср 05.08). Правило Сергея 03.08.2026 для Блоссом и Колортек."""
    d=inv+timedelta(days=1)
    while not is_working(d, six): d+=timedelta(days=1)
    for _ in range(skip):
        d+=timedelta(days=1)
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
