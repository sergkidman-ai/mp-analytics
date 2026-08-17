# поток: rev
"""feedback_bot/feedback_cycle.py — цикл автоматизации ответов, вызывается systemd-таймером раз в 2ч.

Шаги строго последовательно, каждый в своём try/except (частичный сбой не должен ронять весь цикл —
как в collectors/feedback_collect_all.py):
  1. Сбор по всем каналам (collectors.feedback_collect_all).
  1b. Обновление контента карточек WB, если он старше FEEDBACK_CARDS_MAX_AGE_DAYS — без него у части
     SKU (весь wb_acc2 до 27.07) CARD_DATA пуст и вопрос уходит на человека без нужды.
  1c. Пересборка индекса совместимости (reports.compat_index) — СРАЗУ ПОСЛЕ карточек, чтобы новые
     карточки попали в подбор в тот же цикл. Гейт COMPAT_INDEX_MAX_AGE_HOURS (по умолч. 24): сборка
     идёт ~75 секунд, гонять её каждые 2 часа впустую незачем. Провал шага цикл не роняет, а сборка
     транзакционная — старый индекс остаётся рабочим.
  2. Пометка старых неотвеченных вопросов (>30 дней) флагом skipped_old — не генерируем, не шлём
     в модерацию (отзывам возрастной лимит не нужен, весь бэклог отзывов разбирается капельно).
  3. Генерация черновиков (reports.feedback_today.run) — draft_route auto/review/human.
  3b. Слив хвоста старой схемы лимита (state='deferred') — идёт ПЕРЕД авто-отправкой, ручное решение
     оператора вперёд массовой рассылки. Новые карточки в 'deferred' не попадают (лимит их не режет).
  4. Автоотправка позитив-шаблонов отзывов (collectors.feedback_autosend) — порция 5 на КАЖДУЮ
     категорию канала за цикл (свежие ≤7 дней / бэклог) + пауза 3–5 сек, свежие первыми. Дневной
     лимит FEEDBACK_BACKLOG_DAILY_CAP (150/канал) применяется ТОЛЬКО к бэклогу.
     Пустые Ozon-отзывы отсеиваются заранее (площадка их не принимает).
  5. Пуш карточек модерации в Telegram капом FEEDBACK_MOD_CYCLE_BATCH/цикл (переиспользуем
     tg_moderation.send_batch — раньше авторассылку карточек убирали из-за флуда, теперь капается).

Запуск:  ./venv/bin/python feedback_bot/feedback_cycle.py [--no-send]

--no-send (или FEEDBACK_CYCLE_NO_SEND=1) — прогон БЕЗ отправки: сбор, индекс, черновики считаются
как обычно, но шаги 3b/4 (публикация ответов на площадках) и 5 (карточки в Telegram) пропускаются.
Для ручных проверок: обычный ручной прогон — боевой, он публикует ответы покупателям.
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


def _index_line():
    """Одна строка о состоянии индекса совместимости для лога цикла (возраст + объём)."""
    try:
        from reports import compat_index
        m = compat_index.meta()
    except Exception as e:
        return f"состояние неизвестно ({type(e).__name__})"
    if not m:
        return "НЕ СОБРАН"
    return (f"возраст {m['age_hours']:.1f} ч, моделей {m['models_total']}, "
            f"листингов {m['items_total']}")


def _unanswered_total():
    """Сколько содержательных обращений висит без ответа на площадках — контекст для алерта."""
    try:
        r = db.query("""SELECT count(*) n FROM raw_feedback
            WHERE is_answered=false AND NOT skipped_old AND posted_at IS NULL
            AND created_at > now() - make_interval(days => %s)""", (DRAFT_SINCE_DAYS,))
        return r[0]["n"] if r else None
    except Exception:                                        # noqa: BLE001
        return None


def main(no_send=None):
    no_send = (os.environ.get("FEEDBACK_CYCLE_NO_SEND", "0") == "1") if no_send is None else no_send
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    _log(f"=== цикл начат {started}{' · РЕЖИМ БЕЗ ОТПРАВКИ (--no-send)' if no_send else ''} ===")

    # Шаг 0: провайдеры ДО генерации. Пустой баланс — главная причина простоя (13.08.2026, четыре
    # дня молча падали все вопросы), поэтому ругаемся заранее, а не после впустую сожжённого цикла.
    # Цикл не прерываем: умер один провайдер — второй должен доработать свою часть очереди.
    from feedback_bot import health
    _step("0/5 предполёт: балансы и доступность моделей", health.preflight)

    from collectors import feedback_collect_all
    _step("1/5 сбор по каналам", feedback_collect_all.main)

    from collectors import wb_card_content
    _step("1b/5 контент карточек WB (по гейту свежести)", wb_card_content.refresh_if_stale)

    from reports import compat_index
    built = _step("1c/5 индекс совместимости (по гейту возраста)", compat_index.rebuild_if_stale)
    _log(f"индекс совместимости: {'пересобран, ' if built else 'не требовался, '}{_index_line()}")

    _step("2/5 skipped_old для старых вопросов", _mark_skipped_old)

    from reports import feedback_today
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - DRAFT_SINCE_DAYS * 86400))
    _step("3/5 генерация черновиков", feedback_today.run, since=since)

    from feedback_bot import tg_moderation
    auto = None
    if no_send:
        # Всё, что уходит наружу (площадки + карточки оператору), в тестовом прогоне пропускаем.
        # Очередь модерации при этом наполняется — карточки уйдут следующим боевым циклом.
        _log("3b–5/5 ПРОПУЩЕНЫ (--no-send): публикация на площадках и карточки в Telegram")
    else:
        _step("3b/5 слив хвоста старой схемы лимита (deferred)", tg_moderation.flush_deferred)

        from collectors import feedback_autosend
        auto = _step("4/5 авто-отправка позитив-шаблонов", feedback_autosend.run)

        sent = _step("5/5 карточки модерации в Telegram", tg_moderation.send_batch, limit=MOD_CYCLE_BATCH)
        _log(f"карточек отправлено: {sent if sent is not None else 0}")

    # Итог: если хоть что-то НЕ ушло — сказать об этом с причиной. Без этого цикл рапортует OK
    # даже когда все до одного ответа провалились.
    _step("итог: контроль неотправленного", health.report_cycle,
          fails=(feedback_today.LAST_RUN or {}).get("fails"),
          drafts=(feedback_today.LAST_RUN or {}).get("drafts", 0),
          autosend=(auto if not no_send else None),
          pending=_unanswered_total())

    _log("=== цикл завершён ===")


if __name__ == "__main__":
    main(no_send=("--no-send" in sys.argv) or None)
