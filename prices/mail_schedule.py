# поток: prc — расписание прихода прайса, выученное из истории писем (`prc_mail_arrival`).
"""Когда этого поставщика ждать и когда пора беспокоиться.

Сторож `ops.prc_price_watch` умел реагировать только на пришедшее письмо, поэтому тишина
Одиссея с 26 по 28 августа не была событием ни для кого. Модуль даёт обратный сигнал: сегодня
рабочий день, обычное время прихода прошло, письма нет — значит, прайс просрочен.

Общего срока «после обеда всем скопом» быть не может: по замеру 01.07–28.08 Кактус присылает
каждый рабочий день к 10:42, Колортек — к 09:44, но с нормальными пропусками до двух дней,
Одиссей — к 11:17, а Сакура вообще приходит не каждый день. Поэтому срок у каждого свой и
считается из его же истории: расписание поставщика меняется, и правило, записанное руками,
устареет молча (решение Сергея 28.08.2026 — учить из почты).

Порог срабатывания — три условия сразу:
  · сегодня день недели, в который поставщик вообще присылает;
  · текущее время позже `deadline` — личного срока ожидания;
  · рабочих дней с последнего письма не меньше обычного разрыва `gap_days`.
Третье условие и отличает «Кактус пропустил день» (аномалия: у него разрыв всегда 1) от
«Колортек пропустил день» (норма: у него бывает и два).

Проверка: ./venv/bin/python -m prices.mail_schedule
"""
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "/opt/mp-analytics")

from core.db import query

MSK = timezone(timedelta(hours=3))
# Ждём дольше самого позднего наблюдения: поставщик имеет право задержаться на час, и будить
# человека ровно в медиану значит звонить ему через день.
GRACE_AFTER_P90 = 60                       # минут после p90 времени прихода
GRACE_AFTER_MAX = 30                       # ... но и после «самого позднего за историю»
LATEST_ALERT = 20 * 60                     # позже 20:00 не тревожим: рабочий день кончился
# Раньше 10:00 не тревожим никогда. Булат и Сакура шлют ночью (первое письмо дня в 02:33 и
# 01:00), и по их истории срок выходит под утро — но сторож просыпается только в 07:09, и до
# первых прогонов «письма сегодня нет» означает не пропажу, а то, что ночь ещё не кончилась.
EARLIEST_ALERT = 10 * 60
# Меньше этого истории не хватает даже на «обычное время»: у Булата и Солюшнс принта её пока
# три недели, и сторож про них молчит, пока не накопится.
MIN_DAYS = 10
MIN_SPAN = 14
FREQUENT_WEEKDAYS = 4                      # присылает в 4+ дня недели -> ждём все будни


def minutes(dt):
    return dt.hour * 60 + dt.minute


def hhmm(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def pct(values, p):
    """Перцентиль «не выше» на маленькой выборке: 90-й от 10 значений — девятое по счёту."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def bdays(start, end):
    """Рабочих дней между датами, не считая саму `start`. Выходные поставщики не работают."""
    n, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def arrivals(key):
    """История приходов: [(дата, время первого письма в минутах)] по возрастанию."""
    rows = query("""select letter_date, first_at at time zone 'Europe/Moscow' as msk
                      from prc_mail_arrival where supplier_key = %s order by letter_date""",
                 (key,))
    return [(r["letter_date"], minutes(r["msk"])) for r in rows]


def schedule(key, history=None):
    """Что мы знаем о расписании поставщика. Всегда возвращает dict, даже когда истории нет."""
    hist = arrivals(key) if history is None else history
    out = {"key": key, "days": len(hist), "enough": False, "weekdays": set(),
           "deadline": None, "gap_days": None, "median": None, "latest": None,
           "last_date": hist[-1][0] if hist else None,
           "last_time": hist[-1][1] if hist else None}
    if not hist:
        return out
    span = (hist[-1][0] - hist[0][0]).days + 1
    out["enough"] = len(hist) >= MIN_DAYS and span >= MIN_SPAN
    times = [t for _, t in hist]
    out["median"], out["latest"] = pct(times, 0.5), max(times)
    out["deadline"] = max(EARLIEST_ALERT,
                          min(pct(times, 0.9) + GRACE_AFTER_P90,
                              out["latest"] + GRACE_AFTER_MAX, LATEST_ALERT))
    seen = {}
    for day, _ in hist:
        seen[day.weekday()] = seen.get(day.weekday(), 0) + 1
    days = {wd for wd, n in seen.items() if n >= 2 and wd < 5}
    # Присылает почти каждый будний день — считаем, что ждём его всегда: единственный вторник
    # без письма за два месяца не повод вычёркивать вторник из расписания.
    out["weekdays"] = set(range(5)) if len(days) >= FREQUENT_WEEKDAYS else days
    gaps = [bdays(a, b) for (a, _), (b, _) in zip(hist, hist[1:])]
    out["gap_days"] = max(1, pct(gaps, 0.9) or 1)
    return out


def overdue(key, now=None, history=None):
    """Прайс просрочен? -> None либо факты для сообщения.

    Считаем ровно по трём условиям из шапки модуля. Отдельно возвращаем `silent` — сколько
    рабочих дней молчит поставщик: в сообщении это главное число.
    """
    now = now or datetime.now(MSK)
    info = schedule(key, history)
    if not info["enough"] or now.weekday() not in info["weekdays"]:
        return None
    today = now.date()
    if info["last_date"] >= today:                 # письмо сегодня уже было
        return None
    if minutes(now) < info["deadline"]:
        return None
    silent = bdays(info["last_date"], today)
    if silent < info["gap_days"]:
        return None
    return {**info, "silent": silent, "today": today}


def main():
    from prices.profiles import PROFILES
    from prices import unprocessed
    keys = sorted(set(PROFILES) | set(unprocessed.SUPPLIERS))
    wd = "пн вт ср чт пт сб вс".split()
    print(f"{'поставщик':<14} {'дней':>4} {'обычно':>7} {'позже не':>9} {'срок':>6} "
          f"{'разрыв':>6}  дни недели")
    for key in keys:
        s = schedule(key)
        if not s["days"]:
            print(f"{key:<14} истории нет")
            continue
        mark = "" if s["enough"] else "  (истории мало — молчим)"
        print(f"{key:<14} {s['days']:>4} {hhmm(s['median']):>7} {hhmm(s['latest']):>9} "
              f"{hhmm(s['deadline']):>6} {s['gap_days']:>6}  "
              f"{' '.join(wd[d] for d in sorted(s['weekdays'])) or '—'}{mark}")


if __name__ == "__main__":
    main()
