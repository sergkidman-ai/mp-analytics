# поток: prc
"""Freshness of posted Solutions Print enters against the daily cabinet schedule."""
from datetime import datetime, time, timedelta, timezone

from core.db import query

SUPPLIERS = {"s_print_msk"}
MSK = timezone(timedelta(hours=3))
RUN_TIMES = (time(8, 7), time(13, 12), time(18, 17))
# Matches prc-solutionsprint.service TimeoutStartSec=2h. The run may update cards
# in paced batches; an in-progress run must not be mistaken for a missed load.
COMPLETION_GRACE = timedelta(hours=2)


def expected_slot(now):
    """Latest run whose completion deadline has passed, including weekends."""
    now = now.astimezone(MSK)
    slots = [datetime.combine(now.date() - timedelta(days=day), at, MSK)
             for day in (0, 1) for at in RUN_TIMES]
    return max(slot for slot in slots if slot + COMPLETION_GRACE <= now)


def evaluate(now, last_load):
    now = now.astimezone(MSK)
    slot = expected_slot(now)
    # moment is the source/plan time, not DB insertion time. Completing recovery
    # of an old frozen plan does not satisfy a newer scheduled refresh.
    if last_load and last_load["moment"].astimezone(MSK) >= slot:
        return None
    return {"slot": slot, "deadline": slot + COMPLETION_GRACE,
            "last_load": last_load, "now": now}


def overdue(key, now=None):
    if key not in SUPPLIERS:
        return None
    now = now or datetime.now(MSK)
    rows = query("""SELECT id, moment, created_at, docs, rows_loaded
                      FROM prc_price_load
                     WHERE supplier_key=%s AND source_kind='cabinet'
                       AND status='ok' AND dry_run=false
                       AND docs>0 AND rows_loaded>0 AND moment<=%s
                     ORDER BY moment DESC, id DESC LIMIT 1""", (key, now))
    return evaluate(now, rows[0] if rows else None)


def alert_text(profile, late):
    last = late["last_load"]
    last_text = (last["moment"].astimezone(MSK).strftime("%d.%m %H:%M")
                 if last else "успешных загрузок из ЛК нет")
    return (f"⏰ {profile.title} — не обновлены оприходования из ЛК.\n"
            "Автозагрузка ежедневно: 08:07 / 13:12 / 18:17 МСК.\n"
            f"Запуск {late['slot']:%d.%m %H:%M}: успешного обновления нет "
            f"к {late['deadline']:%H:%M} МСК.\n"
            f"Последняя успешная загрузка: {last_text}.\n"
            "Проверить prc-solutionsprint.service и доступ в ЛК; "
            "прайс вручную не загружать.")
