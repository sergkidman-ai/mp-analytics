# поток: rev
"""collectors/feedback_collect_all.py — единый сбор фидбека по ВСЕМ подключённым каналам в raw_feedback.

Каналы (по факту API-доступа на 2026-07):
  wb_acc1   — отзывы + вопросы            (доступ есть)
  wb_acc2   — отзывы + вопросы            (токен без scope «Вопросы и отзывы» → 401, пропускаем)
  oz_acc1   — отзывы(Premium) + вопросы   (доступ есть)
  oz_acc2   — только вопросы              (нет Premium → отзывы 403, коллектор их сам пропускает)
  ya_acc1   — отзывы                      (у Яндекса вопросов в этом API нет)

Каждый канал изолирован try/except — сбой одного не срывает остальные. Запуск:
  ./venv/bin/python collectors/feedback_collect_all.py
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from collectors import wb_feedbacks, ozon_feedbacks, yandex_feedbacks  # noqa: E402


def _safe(label, fn, *a):
    try:
        fn(*a)
    except Exception as e:
        print(f"  [{label}] пропущен: {type(e).__name__}: {str(e)[:140]}", flush=True)


def main():
    print("=== Сбор фидбека по всем каналам ===", flush=True)
    _safe("wb_acc1", wb_feedbacks.main, "wb_acc1")
    _safe("wb_acc2", wb_feedbacks.main, "wb_acc2")     # ждёт токен со scope «Вопросы и отзывы»
    _safe("oz_acc1", ozon_feedbacks.main, "oz_acc1")
    _safe("oz_acc2", ozon_feedbacks.main, "oz_acc2")   # отзывы 403 (нет Premium) — внутри пропустятся
    _safe("ya_acc1", yandex_feedbacks.main)
    print("=== Готово ===", flush=True)


if __name__ == "__main__":
    main()
