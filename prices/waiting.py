# поток: prc
# -*- coding: utf-8 -*-
"""
Лист ожидания цветовых комплектов: что из ждущих строк может собраться сегодня.

Правило 5 (Сергей, 06.08) кладёт неполный комплект на полку: цвета доедут — заведём.
Но полка бесполезна, если о ней не напоминать: строка `decision = 'partial'` пролежит
там вечно, пока человек сам не вспомнит, что ждал жёлтый. Поэтому каждый прогон прайса
сверяет полку с тем, что пришло, и показывает найденных соседей по серии (задача
Сергея 07.08: «зашёл PFI121M, а в листе ожидания или в ЧС есть другие PFI121 — выводим»).

Сосед ищется двумя независимыми способами, и хватает любого:
  * **по артикулу** — та же длина, общий префикс от 5 знаков, расходится только хвост
    до 3 знаков: `CS-PFI121M` ↔ `CS-PFI121C`, `CS-EPT49N200` ↔ `CS-EPT49N100`;
  * **по моделям принтеров из названия** — тот же механизм, что собирает комплекты
    (`novelty.models` + `novelty.namespace`): у разных поставщиков артикулы разные,
    а «для Canon iR C3326» общее. Тип товара при этом обязан совпадать — флакон тонера
    не закрывает недостающий цвет картриджа.

Найденный сосед — это подсказка человеку, а не решение: строку в работу возвращает он
кнопкой. Особый случай — сосед из чёрного списка: цвет мы забраковали сами, комплект
не соберётся никогда, и такую строку разумнее закрыть, а не ждать.
"""
import re

from .features import color as feature_color
from .novelty import COLOR_NAMES, color, models, namespace

MIN_STEM = 5            # код короче — уже не серия, а случайное созвучие
# Цветовые хвосты артикулов. Длинные раньше коротких: BK должен сработать раньше K.
COLOR_TAILS = ("BKM", "MBK", "LLK", "BK", "LC", "LM", "LK", "PK", "MK", "GY", "GR",
               "C", "M", "Y", "K")

# Откуда сосед, по убыванию полезности. Порядок задаёт и то, какая метка победит,
# если один и тот же артикул нашёлся дважды.
SOURCE_RANK = {"blacklist": 0, "rejected": 1, "waiting": 2, "price": 3, "ms": 4}
SOURCE_NAMES = {
    "blacklist": "в чёрном списке",
    "rejected": "в прайсе есть, но мы такое не берём (объём/тип/категория)",
    "waiting": "тоже в листе ожидания",
    "price": "в сегодняшнем прайсе",
    "ms": "в сегодняшнем прайсе, уже заведён в МС",
}
# Цвет, забракованный правилами ассортимента, комплект не закрывает: флакон на 500 г мы
# не берём, и «жёлтый есть» тут неправда.
DEAD_SOURCES = ("blacklist", "rejected")
REJECT_REASONS = ("bulk_toner", "bulk_ink", "cleaning", "refillable", "category_off")


def key(article):
    """Артикул к сравнимому виду: заглавные, без дефисов и пробелов."""
    return re.sub(r"[^0-9A-ZА-Я]", "", str(article or "").upper())


def _stem(article):
    """Артикул без цветового хвоста: CSPFI121M -> (CSPFI121, 'M'). Хвоста нет -> (артикул, '')."""
    for tail in COLOR_TAILS:
        if article.endswith(tail) and len(article) - len(tail) >= MIN_STEM:
            return article[:-len(tail)], tail
    return article, ""


def same_article_series(a, b):
    """Похожи ли артикулы на цвета одной серии.

    Два признака, любого достаточно:
      * расходятся ровно в одном знаке при равной длине — `CS-EPT49N100` ↔ `CS-EPT49N400`,
        `CS-PFI121M` ↔ `CS-PFI121C`. Именно один: расхождение в двух знаках — это уже
        другая модель (`CS-PFI121M` и `CS-PFI102M` — разные принтеры, не цвета);
      * совпадает основа при РАЗНЫХ цветовых хвостах — `CS-TN217BK` ↔ `CS-TN217C`,
        где хвосты разной длины и посимвольное сравнение не работает.
    """
    if not a or not b or a == b or len(a) < MIN_STEM or len(b) < MIN_STEM:
        return False
    if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
        return True
    stem_a, tail_a = _stem(a)
    stem_b, tail_b = _stem(b)
    return bool(tail_a and tail_b and tail_a != tail_b and stem_a == stem_b)


def same_name_series(row, other):
    """Одна ли это серия по названию: общая модель принтера при одинаковом типе товара."""
    if namespace(row) != namespace(other):
        return False
    mine, theirs = models(row), models(other)
    return bool(mine and theirs and mine & theirs)


def hue(item):
    """Цвет строки кодом. Разбор признаков знает сокращения («M пурп.пигм.»), которых
    не знает разбор комплектов, поэтому он первый; колонка `color` — запасной путь."""
    return feature_color(item.get("name")) or color(item.get("name")) or item.get("color")


def missing(colors_have):
    """Каких цветов комплекту не хватает — словами."""
    return [COLOR_NAMES[c] for c in ("BK", "C", "M", "Y") if c not in colors_have]


def find(waiting, pool):
    """Сверить полку с тем, что есть вокруг.

    waiting — строки на полке (`id`, `article`, `name`, …), pool — всё, чем комплект может
    закрыться: сегодняшний прайс, чёрный список, другие ждущие строки. У элемента пула
    обязателен `source` из SOURCE_NAMES; `name` может отсутствовать (в ЧС только артикулы) —
    тогда цвет соседа неизвестен, но сам факт «этот артикул забракован» уже ценен.

    -> [{**строка, "siblings": [...], "missing": [...], "dead": bool}] — только те строки,
    у которых соседи нашлись. `missing` считаем по ЖИВЫМ цветам (забракованный цвет комплект
    не закрывает), `dead` = какой-то из недостающих цветов есть только в чёрном списке,
    то есть ждать его бессмысленно — мы сами его и забраковали.
    """
    prepared = [(item, key(item.get("article")), hue(item)) for item in pool]
    out = []
    for row in waiting:
        row_key, own = key(row.get("article")), hue(row)
        found = {}
        for item, item_key, code in prepared:
            if item_key == row_key or item.get("id") == row.get("id"):
                continue
            # Тот же цвет комплект не двигает: это просто другой бренд того же картриджа.
            # Цвет неизвестен (в ЧС одни артикулы) — показываем, пусть человек посмотрит.
            if own and code and code == own:
                continue
            if not (same_article_series(row_key, item_key) or same_name_series(row, item)):
                continue
            best = found.get(item_key)
            if best is None or SOURCE_RANK[item["source"]] > SOURCE_RANK[best["source"]]:
                found[item_key] = item
        if not found:
            continue
        have = {own} - {None}
        alive = set(have)
        for item in found.values():
            code = hue(item)
            if code:
                have.add(code)
                if item["source"] not in DEAD_SOURCES:
                    alive.add(code)
        siblings = sorted(found.values(), key=lambda i: (SOURCE_RANK[i["source"]],
                                                         key(i.get("article"))))
        # Забракованный сосед без названия: цвет не определить, значит и «ждать нечего»
        # не доказать. Не молчим об этом — человек посмотрит на артикул и решит сам.
        suspect = any(s["source"] in DEAD_SOURCES and not hue(s) for s in siblings)
        out.append(dict(row, siblings=siblings, missing=missing(alive),
                        dead=alive < have, suspect=suspect))
    return out


def shelf(supplier_key=None):
    """Строки на полке ожидания — по всем поставщикам сразу.

    Комплект собираем из разных поставщиков (решение Сергея 06.08), поэтому полка общая:
    жёлтый Кактуса закрывает серию Колортека. Фильтр по поставщику — только для отчёта прогона.
    """
    from core.db import query
    rows = query("""SELECT id, supplier_key, article, name, color, price_rub,
                           ms_code, ms_name, link
                      FROM prc_novelty WHERE decision = 'partial'
                     ORDER BY supplier_key, name""")
    return [r for r in rows if not supplier_key or r["supplier_key"] == supplier_key]


def pool(price_rows=None):
    """Всё, чем комплект может закрыться: прайс + чёрный список + сама полка.

    `price_rows` — строки текущего прогона (`article`, `name`, `reason`); если их не дали
    (вкладка на дашборде), берём последний прогон каждого поставщика из журнала. Строка,
    попавшая в оприходование, помечается `ms` — этот цвет у нас уже заведён, и комплект
    он закрывает лучше всех.
    """
    from core.db import query
    out = []
    if price_rows is None:
        price_rows = query("""
            SELECT r.article, r.name, r.reason, r.status
              FROM prc_price_row r
              JOIN (SELECT supplier_key, max(id) id FROM prc_price_load
                     WHERE status = 'ok' GROUP BY supplier_key) last ON last.id = r.load_id""")
    for row in price_rows:
        reason = row.get("reason")
        source = "ms" if row.get("status") == "loaded" else "price"
        if reason == "blacklisted":                  # цвет в прайсе есть, но забракован нами
            source = "blacklist"
        elif reason in REJECT_REASONS:               # есть, но по правилам ассортимента не наш
            source = "rejected"
        out.append({"article": row.get("article"), "name": row.get("name"), "source": source})
    # У чёрного списка одни артикулы, а без названия не виден цвет — и «ждать нечего»
    # не докажешь. Название добираем из прайсов: забракованный артикул когда-то в них был.
    seen = {key(r["article"]): r["name"] for r in query("""
        SELECT DISTINCT ON (upper(article)) article, name FROM prc_price_row
         ORDER BY upper(article), load_id DESC""")}
    black = query("SELECT article FROM prc_blacklist")
    out += [{"article": r["article"], "name": seen.get(key(r["article"])), "source": "blacklist"}
            for r in black]
    out += [dict(r, source="waiting") for r in shelf()]
    return out


def verdict(match):
    """Что делать со строкой — словами, одинаково в консоли, файле и на вкладке."""
    if match["dead"]:
        return "ждать нечего: недостающий цвет забракован (ЧС или правила ассортимента)"
    if match["suspect"]:
        return "часть серии в чёрном списке, цвет по артикулу не определить — посмотреть глазами"
    return "цвета появились — можно вернуть в работу"


def summary(matches):
    """Одна строка для консоли прогона."""
    if not matches:
        return "новых цветов к ждущим строкам не пришло"
    dead = sum(1 for m in matches if m["dead"])
    suspect = sum(1 for m in matches if m["suspect"] and not m["dead"])
    tail = f", из них ждать нечего {dead}" if dead else ""
    tail += f", под вопросом {suspect} (соседи в ЧС, цвет неизвестен)" if suspect else ""
    return f"соседи по серии нашлись у {len(matches)} ждущих строк{tail}"
