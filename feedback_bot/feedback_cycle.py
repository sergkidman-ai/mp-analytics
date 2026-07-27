# поток: rev
"""feedback_bot/feedback_cycle.py — цикл автоматизации ответов, вызывается systemd-таймером раз в 2ч.

Шаги строго последовательно, каждый в своём try/except (частичный сбой не должен ронять весь цикл —
как в collectors/feedback_collect_all.py):
  1. Сбор по всем каналам (collectors.feedback_collect_all).
  1b. Обновление контента карточек WB, если он старше FEEDBACK_CARDS_MAX_AGE_DAYS — без него у части
     SKU (весь wb_acc2 до 27.07) CARD_DATA пуст и вопрос уходит на человека без нужды.
  2. Пометка старых неотвеченных вопросов (>30 дней) флагом skipped_old — не генерируем, не шлём
     в модерацию (отзывам возрастной лимит не нужен, весь бэклог отзывов разбирается капельно).
  3. Генерация черновиков (reports.feedback_today.run) — draft_route auto/review/human.
  4. Автоотправка позитив-шаблонов отзывов (collectors.feedback_autosend) — пейсинг 5/канал/цикл.
  5. Пуш карточек модерации в Telegram капом FEEDBACK_MOD_CYCLE_BATCH/цикл (переиспользуем
     tg_moderation.send_batch — раньше авторассылку карточек убирали из-за флуда, теперь капается).

Запуск:  ./venv/bin/python feedback_bot/feedback_cycle.py
"""
import os
import sys
import time
import pathlib
import traceback

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                          # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                      # noqa: E402

MOD_CYCLE_BATCH = int(os.environ.get("FEEDBACK_MOD_CYCLE_BATCH", "3"))
DRAFT_SINCE_DAYS = int(os.environ.get("FEEDBACK_DRAFT_SINCE_DAYS", "30"))


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] feedback_cycle: {msg}", flush=True)


def _step(label, fn, *a, **kw):
    try:
        r = fn(*a, **kw)
        _log(f"OK  {label}")
        return r
    except Exception as e:
        _log(f"FAIL {label}: {type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc()
        return None


def _mark_skipped_old():
    """Вопросы без ответа старше DRAFT_SINCE_DAYS дней → skipped_old=true (не трогаем отзывы)."""
    n = db.execute("""UPDATE raw_feedback SET skipped_old=true
        WHERE kind='question' AND is_answered=false AND NOT skipped_old
        AND created_at < now() - make_interval(days => %s)""", (DRAFT_SINCE_DAYS,))
    return n


def main():
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    _log(f"=== цикл начат {started} ===")

    from collectors import feedback_collect_all
    _step("1/5 сбор по каналам", feedback_collect_all.main)

    from collectors import wb_card_content
    _step("1b/5 контент карточек WB (по гейту свежести)", wb_card_content.refresh_if_stale)

    _step("2/5 skipped_old для старых вопросов", _mark_skipped_old)

    from reports import feedback_today
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - DRAFT_SINCE_DAYS * 86400))
    _step("3/5 генерация черновиков", feedback_today.run, since=since)

    from collectors import feedback_autosend
    _step("4/5 авто-отправка позитив-шаблонов", feedback_autosend.run)

    from feedback_bot import tg_moderation
    sent = _step("5/5 карточки модерации в Telegram", tg_moderation.send_batch, limit=MOD_CYCLE_BATCH)
    _log(f"карточек отправлено: {sent if sent is not None else 0}")

    _log("=== цикл завершён ===")


if __name__ == "__main__":
    main()
