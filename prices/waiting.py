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
  * **по артикулу** — расхождение ровно в одном знаке при равной длине (`CS-PFI121M` ↔
    `CS-PFI121C`) либо общая основа при разных цветовых хвостах (`CS-TN217BK` ↔ `CS-TN217C`);
  * **по моделям принтеров из названия** — тот же механизм, что собирает комплекты
    (`novelty.models` + `novelty.namespace`): у разных поставщиков артикулы разные,
    а «для Canon iR C3326» общее. Тип товара при этом обязан совпадать — флакон тонера
    не закрывает недостающий цвет картриджа.

Найденный сосед — это подсказка человеку, а не решение: строку в работу возвращает он
кнопкой. **Сосед из чёрного списка возвращается вместе с ней** (решение Сергея 08.08):
в ЧС артикул попал одиночкой, которой не с чем встать в пару, и появление второй половины
комплекта отменяет причину отсева. `revive()` снимает такого соседа с ЧС и заводит в новинки
рядом со ждущей строкой.
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
# Комплект не закрывает только то, что мы не берём ПО СУТИ товара: промывка остаётся
# промывкой, сколько её ни жди.
#
# bulk_toner / bulk_ink с 09.08 никто не ставит (граница 150 г снята): флакон теперь
# оценивается по дозе полной заправки конкретной модели. Причины оставлены в списке
# ради строк старых прогонов, которые всё ещё лежат в журнале.
#
# Чёрный список сюда НЕ входит (решение Сергея 08.08). В ЧС артикул попал как одиночка,
# которой не с чем встать в пару, — это «пока не берём», а не «забраковано навсегда».
# Нашёлся недостающий цвет — комплект собрался, и в новинки едет ПАРА: ждущая строка
# и сосед из ЧС (её пример: `CS-I-CL441C` + `CS-I-PG440`).
DEAD_SOURCES = ("rejected",)
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


def _better(item, best):
    """Из двух записей об одном артикуле оставить полезнейшую.

    Сначала по источнику (`SOURCE_RANK`), а при равном источнике — ту, где известен
    поставщик: один и тот же забракованный артикул приходит и строкой прайса, и записью ЧС,
    но вернуть его в новинки можно только зная, чей это прайс.
    """
    rank, best_rank = SOURCE_RANK[item["source"]], SOURCE_RANK[best["source"]]
    if rank != best_rank:
        return rank > best_rank
    return bool(item.get("supplier_key")) and not best.get("supplier_key")


def find(waiting, pool):
    """Сверить полку с тем, что есть вокруг.

    waiting — строки на полке (`id`, `article`, `name`, …), pool — всё, чем комплект может
    закрыться: сегодняшний прайс, чёрный список, другие ждущие строки. У элемента пула
    обязателен `source` из SOURCE_NAMES; `name` может отсутствовать (в ЧС только артикулы) —
    тогда цвет соседа неизвестен, но сам факт «этот артикул забракован» уже ценен.

    -> [{**строка, "siblings": [...], "revive": [...], "missing": [...], "dead": bool}] —
    только те строки, у которых соседи нашлись. `missing` считаем по ЖИВЫМ цветам (флакон
    на 500 г цвет не закрывает), `dead` = недостающий цвет есть только среди отсеянного
    по правилам ассортимента. `revive` — соседи из ЧС: они вернутся в работу вместе со строкой.
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
            if best is None or _better(item, best):
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
        # Отсеянный по правилам сосед без названия: цвет не определить, значит и «ждать
        # нечего» не доказать. Не молчим об этом — человек посмотрит на артикул и решит сам.
        suspect = any(s["source"] in DEAD_SOURCES and not hue(s) for s in siblings)
        # Сосед из ЧС возвращается в работу вместе со строкой — но только тот, которого мы
        # видели в прайсе: без поставщика и названия заводить в новинки нечего, а сам по себе
        # артикул из присланного списка ещё не значит, что такой товар у поставщика есть.
        black = [s for s in siblings if s["source"] == "blacklist"]
        for sib in black:
            sib["can_revive"] = bool(sib.get("supplier_key") and sib.get("name"))
        out.append(dict(row, siblings=siblings, missing=missing(alive), dead=alive < have,
                        suspect=suspect,
                        revive=[s for s in black if s["can_revive"]],
                        unknown_black=[s for s in black if not s["can_revive"]]))
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


def pool(price_rows=None, supplier_key=None):
    """Всё, чем комплект может закрыться: прайс + чёрный список + сама полка.

    `price_rows` — строки текущего прогона (`article`, `name`, `reason`); если их не дали
    (вкладка на дашборде), берём последний прогон каждого поставщика из журнала. Строка,
    попавшая в оприходование, помечается `ms` — этот цвет у нас уже заведён, и комплект
    он закрывает лучше всех.

    Поставщика и цену тащим с собой не для показа: `revive()` заводит соседа из ЧС
    в новинки, а для этого нужен профиль его поставщика.
    """
    from core.db import query
    out = []
    if price_rows is None:
        price_rows = query("""
            SELECT r.article, r.name, r.reason, r.status, r.price_rub, r.price_src,
                   l.supplier_key
              FROM prc_price_row r
              JOIN (SELECT supplier_key, max(id) id FROM prc_price_load
                     WHERE status = 'ok' GROUP BY supplier_key) last ON last.id = r.load_id
              JOIN prc_price_load l ON l.id = r.load_id""")
    # Причина в журнале прогона — история, а не приговор: снятый с ЧС артикул так и остаётся
    # там «blacklisted». Что в ЧС сейчас, знает только сама таблица, её и спрашиваем.
    from .blacklist import load_set, norm as black_norm
    in_black = load_set()
    for row in price_rows:
        reason = row.get("reason")
        source = "ms" if row.get("status") == "loaded" else "price"
        if reason == "blacklisted" and black_norm(row.get("article")) in in_black:
            source = "blacklist"                     # цвет в прайсе есть, но забракован нами
        elif reason in REJECT_REASONS:               # есть, но по правилам ассортимента не наш
            source = "rejected"
        out.append({"article": row.get("article"), "name": row.get("name"), "source": source,
                    "supplier_key": row.get("supplier_key") or supplier_key,
                    "price": row.get("price_rub") if row.get("price_rub") is not None
                             else row.get("price"),
                    "price_src": row.get("price_src")})
    # У чёрного списка одни артикулы, а без названия не виден цвет. Название, поставщика
    # и цену добираем из прайсов: забракованный артикул когда-то в них был — и без них
    # `revive()` не сможет завести его в новинки.
    seen = {key(r["article"]): r for r in query("""
        SELECT DISTINCT ON (upper(r.article)) r.article, r.name, r.price_rub, r.price_src,
                                              l.supplier_key
          FROM prc_price_row r JOIN prc_price_load l ON l.id = r.load_id
         ORDER BY upper(r.article), r.load_id DESC""")}
    black = query("SELECT article FROM prc_blacklist")
    for r in black:
        known = seen.get(key(r["article"])) or {}
        out.append({"article": r["article"], "name": known.get("name"), "source": "blacklist",
                    "supplier_key": known.get("supplier_key"), "price": known.get("price_rub"),
                    "price_src": known.get("price_src")})
    out += [dict(r, source="waiting") for r in shelf()]
    return out


def verdict(match):
    """Что делать со строкой — словами, одинаково в консоли, файле и на вкладке."""
    if match["revive"]:
        return (f"комплект собрался: вернуть в работу вместе с {len(match['revive'])} из ЧС "
                f"({', '.join(s['article'] for s in match['revive'])})")
    if match["unknown_black"]:
        return ("часть серии в ЧС, но в прайсах мы её не видели — "
                f"спросить у поставщика ({', '.join(s['article'] for s in match['unknown_black'])})")
    if match["dead"]:
        return "ждать нечего: цвет есть только в том, что мы не берём (объём/тип/категория)"
    if match["suspect"]:
        return "сосед отсеян по правилам, цвет по артикулу не определить — посмотреть глазами"
    return "цвета появились — можно вернуть в работу"


def summary(matches):
    """Одна строка для консоли прогона."""
    if not matches:
        return "новых цветов к ждущим строкам не пришло"
    pairs = sum(1 for m in matches if m["revive"])
    ask = sum(1 for m in matches if m["unknown_black"] and not m["revive"])
    dead = sum(1 for m in matches if m["dead"] and not m["revive"] and not m["unknown_black"])
    tail = f", из них собрались с соседом из ЧС {pairs}" if pairs else ""
    tail += f", спросить у поставщика {ask}" if ask else ""
    tail += f", ждать нечего {dead}" if dead else ""
    return f"соседи по серии нашлись у {len(matches)} ждущих строк{tail}"


def revive(novelty_id):
    """Вернуть ждущую строку в работу вместе с её соседями из чёрного списка.

    Соседа мало снять с ЧС: строки в новинках у него нет — он отсеялся раньше, чем дошёл
    до сверки с каталогом. Поэтому заводим его тем же путём, что и обычную новинку
    (`catalog.sync` по профилю его поставщика), чтобы человек увидел подсказки по каталогу.
    Соседа, которого мы никогда не видели в прайсе, заводить не из чего: артикул из
    присланного списка — ещё не товар. Такого в ЧС и оставляем, но называем вслух: может
    оказаться, что у поставщика он есть, просто в прайс не попал.

    -> {"revived": [заведены в новинки], "unlisted": [оставлены в ЧС, в прайсах не было],
    "row": id вернувшейся строки}
    """
    from core.db import execute
    from .blacklist import norm as black_norm
    from .catalog import sync
    from .profiles import get_profile

    row = [r for r in shelf() if r["id"] == novelty_id]
    matches = find(row, pool()) if row else []
    sibs = matches[0]["revive"] if matches else []
    unlisted = [s["article"] for s in matches[0]["unknown_black"]] if matches else []

    revived = []
    for sib in sibs:
        execute("DELETE FROM prc_blacklist WHERE article_norm = %s",
                (black_norm(sib["article"]),))
        profile = get_profile(sib["supplier_key"])
        # Цена: `price_rub` у отсеянной строки часто пустая (её считают на загрузке).
        # Взять `price_src` можно только у рублёвого прайса — у валютного это доллары,
        # и подставлять их как рубли нельзя. Пусто — заполнится следующим прогоном.
        price = sib.get("price")
        if price is None and profile.currency == "RUB":
            price = sib.get("price_src")
        sync([{"name": sib["name"], "article": sib["article"], "price": price}],
             sib["supplier_key"], profile.default_chip, profile.article_re)
        revived.append(sib["article"])
    execute("UPDATE prc_novelty SET decision = 'pending', decided_at = now() WHERE id = %s",
            (novelty_id,))
    return {"revived": revived, "unlisted": unlisted, "row": novelty_id}
