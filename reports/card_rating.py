# поток: rev
"""reports/card_rating.py — рейтинг карточки и вердикт «жива / под убой».

Правило Сергея 24.08.2026: решение о модерации негативного отзыва принимается по ОБЩЕМУ
рейтингу карточки, а не по тексту отзыва.
  * негатив карточку НЕ убил (рейтинг держится) → отвечает человек: карточку ещё спасаем,
    ответ читает будущий покупатель;
  * карточка под убой (рейтинг уже не вытащить) → сухой шаблон-хендофф в чат по QR,
    оператора не трогаем: такую карточку закрывают и заводят новую.

Рейтинг карточки:
  * WB — собственный рейтинг ВБ из `wb_search_report.feedback_rating` (свежий, недельный);
  * Ozon — рейтинг площадки из `ozon_rating` (сбор замер 17.07.2026 вместе с review API);
  * иначе — среднее по нашим собранным отзывам (`raw_feedback`). Сверка на WB: у 284 из 326
    карточек наш агрегат совпал с рейтингом ВБ в пределах 0.3, средняя разница +0.06 —
    считать по своим отзывам можно.

Цена спасения — сколько подряд 5★ нужно, чтобы вернуть карточку к ALIVE:
    k = ceil((ALIVE*n − сумма оценок) / (5 − ALIVE)).
"""
import math
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                       # noqa: E402

ALIVE = 4.6          # карточка продаётся нормально
DEAD = 4.2           # ниже — восстановлению арифметически не подлежит
MAX_RESCUE = 10      # пятёрок подряд, которые ещё считаем реалистичными
MIN_N = 5            # меньше отзывов в нашей базе и нет рейтинга площадки → хоронить нельзя

# сухой шаблон для карточки под убой: без сочувственных абзацев, только маршрут в чат
DEAD_TEXT = ("Здравствуйте! Напишите нам, пожалуйста, в чат по QR-коду на упаковке или в товарном "
             "чеке внутри коробки — разберёмся и решим вопрос.")

_IDX = None


def _load():
    """{(platform, item_id): (n, sum, own_rating|None)} — один запрос на прогон."""
    idx = {}
    for r in db.query("""SELECT platform, item_id::text AS key, count(*) AS n, sum(rating) AS s
                         FROM raw_feedback
                         WHERE kind='review' AND rating IS NOT NULL AND item_id IS NOT NULL
                         GROUP BY 1,2"""):
        idx[(r["platform"], r["key"])] = [int(r["n"]), float(r["s"]), None]
    for r in db.query("""SELECT sku::text AS key, avg_rating AS fr, reviews_count AS rc
                         FROM ozon_rating WHERE avg_rating > 0"""):
        cur = idx.get(("ozon", r["key"]))
        if cur:
            cur[2] = float(r["fr"])
            cur[0] = max(cur[0], int(r["rc"] or 0))
    for r in db.query("""SELECT DISTINCT ON (nm_id) nm_id::text AS key, feedback_rating AS fr
                         FROM wb_search_report WHERE feedback_rating > 0
                         ORDER BY nm_id, period_start DESC"""):
        cur = idx.get(("wb", r["key"]))
        if cur:
            cur[2] = float(r["fr"])
    return idx


def verdict(platform, item_id):
    """→ dict(rating, n, rescue, alive, source). Нет данных по карточке → alive=True
    (не знаем — значит не хороним: пусть смотрит человек)."""
    global _IDX
    if _IDX is None:
        _IDX = _load()
    row = _IDX.get((platform, str(item_id) if item_id is not None else ""))
    if not row:
        return {"rating": None, "n": 0, "rescue": None, "alive": True, "source": "нет данных"}
    n, s, own = row
    rating = own if own is not None else (s / n if n else None)
    if rating is None:
        return {"rating": None, "n": n, "rescue": None, "alive": True, "source": "нет оценок"}
    if own is None and n < MIN_N:
        # своих отзывов слишком мало (карточка старше нашего сбора) — вердикт не выносим
        return {"rating": round(s / n, 2), "n": n, "rescue": None, "alive": True,
                "source": f"мало данных ({n} отз.)"}
    total = own * n if own is not None else s
    rescue = max(0, math.ceil((ALIVE * n - total) / (5 - ALIVE)))
    alive = rating >= ALIVE or (rating >= DEAD and rescue <= MAX_RESCUE)
    return {"rating": round(rating, 2), "n": n, "rescue": rescue, "alive": alive,
            "source": "рейтинг ВБ" if own is not None else "наши отзывы"}


def note(v):
    """Короткая строка для карточки модератора и лога."""
    if v["rating"] is None:
        return f"рейтинг карточки: {v['source']}"
    return (f"карточка {v['rating']}★ ({v['n']} отз., {v['source']}), "
            f"до {ALIVE} нужно {v['rescue']}×5★ → "
            + ("жива, отвечает человек" if v["alive"] else "под убой, сухой шаблон"))
