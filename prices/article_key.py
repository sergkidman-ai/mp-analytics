# поток: prc
"""Ключ артикула поставщика: уникальность и «похожие» написания (правила 43–46 `docs/PRC_RULES.md`).

Артикул в карточке МС пишется РОВНО как в прайсе поставщика (правило 45) — регистр,
разделители и даже русские буквы-двойники, если они стоят в прайсе. По нему матчится строка
прайса, от него зависит, встанет ли товар на остаток и цену при Оприходовании; «исправлять»
его в МС нельзя. Поэтому нормализация здесь нужна НЕ для записи, а только для поиска
столкновений: два артикула, различающиеся лишь регистром или буквой-двойником, для человека
один товар, для МС — разные. Именно так 19.08.2026 родились 9 карточек-клонов.

`art_key()` складывает регистр и кириллические двойники в латиницу. Разные байты при одном
ключе — не приговор, а СТОП: решает человек (правило 46).

Точное совпадение спрашиваем у МС (истина), «похожие» ищем по слепку `ms_product` — иначе
пришлось бы перебирать базу через API на каждую строку.
"""
from core import db, ms_api

# только визуально неотличимые пары: кириллица → латиница
CYR2LAT = {"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O",
           "Р": "P", "Т": "T", "Х": "X", "У": "Y", "а": "a", "с": "c", "е": "e", "о": "o",
           "р": "p", "х": "x", "у": "y"}
_live = None


def art_key(article):
    """Ключ сравнения: верхний регистр, затем гомоглифы в латиницу. Для ПОИСКА, не для записи.

    Порядок важен: сначала регистр. Иначе строчная кириллическая «м» из `cs-thм247`
    не попадёт в карту, а после `.upper()` станет «М» — и клон не найдётся.
    """
    return "".join(CYR2LAT.get(ch, ch) for ch in str(article or "").strip().upper())


def live_rows(refresh=False):
    """Живые карточки с непустым артикулом из слепка ms_product (кэш на процесс)."""
    global _live
    if _live is None or refresh:
        _live = db.query("SELECT ms_id AS id, code, external_code AS ext, article, name "
                         "FROM ms_product WHERE NOT archived AND article <> ''")
    return _live


def clash(article, exclude_ids=()):
    """(exact, near): живые карточки с ТЕМ ЖЕ артикулом и с тем же ключом, но иным написанием.

    exact — нарушение правила 43 (артикул уникален), near — повод остановиться (правило 46).
    """
    art = str(article or "").strip()
    if not art:
        return [], []
    skip = {str(i) for i in exclude_ids}
    exact = [{"id": p["id"], "code": p.get("code"), "ext": p.get("externalCode"),
              "article": p.get("article"), "name": p.get("name")}
             for p in ms_api.get("/entity/product", {"filter": f"article={art}", "limit": 100}).get("rows", [])
             if not p.get("archived") and p["id"] not in skip]
    key = art_key(art)
    near = [dict(r) for r in live_rows()
            if r["id"] not in skip and str(r["article"]).strip() != art and art_key(r["article"]) == key]
    return exact, near


def describe(rows, limit=4):
    return ", ".join(f'{r.get("code") or "—"}/вн.{r.get("ext") or "—"} «{r.get("article")}»'
                     for r in rows[:limit]) + (" …" if len(rows) > limit else "")
