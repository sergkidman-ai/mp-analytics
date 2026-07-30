# поток: rev
"""collectors/feedback_collect_all.py — единый сбор фидбека по ВСЕМ подключённым каналам в raw_feedback.

Каналы (по факту API-доступа на 2026-07):
  wb_acc1   — отзывы + вопросы            (доступ есть)
  wb_acc2   — отзывы + вопросы            (токен без scope «Вопросы и отзывы» → 401, пропускаем)
  oz_acc1   — отзывы(Premium) + вопросы   (доступ есть)
  oz_acc2   — только вопросы              (нет Premium → отзывы 403, коллектор их сам пропускает)
  ya_acc1   — отзывы + вопросы о товарах  (вопросы — отдельный путь /v1/.../goods-questions)

Каждый канал изолирован try/except — сбой одного не срывает остальные. Запуск:
  ./venv/bin/python collectors/feedback_collect_all.py
"""
import sys
import pathlib
from datetime import datetime, timezone

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from collectors import (wb_feedbacks, ozon_feedbacks, yandex_feedbacks,  # noqa: E402
                        yandex_questions)


def _safe(label, fn, *a):
    try:
        fn(*a)
        return True
    except Exception as e:
        print(f"  [{label}] пропущен: {type(e).__name__}: {str(e)[:140]}", flush=True)
        return False


def main():
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"=== Сбор фидбека по всем каналам · {started} ===", flush=True)
    results = [
        _safe("wb_acc1", wb_feedbacks.main, "wb_acc1"),
        _safe("wb_acc2", wb_feedbacks.main, "wb_acc2"),  # ждёт токен со scope «Вопросы и отзывы»
        _safe("oz_acc1", ozon_feedbacks.main, "oz_acc1"),
        _safe("oz_acc2", ozon_feedbacks.main, "oz_acc2"),  # отзывы 403 внутри пропустятся
        _safe("ya_acc1", yandex_feedbacks.main),
        _safe("ya_acc1:questions", yandex_questions.main),
    ]
    ok = sum(results)
    print(f"=== Готово: успешно {ok}/{len(results)} каналов ===", flush=True)
    # Частичные сбои ожидаемы и изолированы, но полностью мёртвый сбор должен быть виден systemd.
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
