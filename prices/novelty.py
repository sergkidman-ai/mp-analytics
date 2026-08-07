# поток: prc
# -*- coding: utf-8 -*-
"""
Отсев новинок: что из ненайденного в МС вообще стоит заводить.

Работает ПОСЛЕ матчинга и чёрного списка, только над строками-новинками
(`not_found` / `ambiguous`). На загрузку оприходований не влияет: этих товаров в МС нет,
речь только о том, что показать человеку.

Правила (Сергей, 06.08.2026):
  1. тонер в больших флаконах — берём только разовую заправку, до 150 г;
  2. промывочная жидкость — не берём;
  3. чернила — тоже до 150 мл;
  4. тонер и чернила «Материалов для заправки» — только полным цветовым комплектом
     (чёрный + голубой + пурпурный + жёлтый); одинокий голубой не нужен;
  5. то же для картриджей, НО их не хороним: неполный комплект уходит в список
     наблюдения — поставщик может дозавести цвета, и тогда мы комплект заведём;
  6. перезаправляемые картриджи — не берём.

Комплект считаем по ВСЕМУ прайсу поставщика, а не по списку новинок: если чёрный у нас
уже заведён, а поставщик привёз к нему три цвета — это как раз тот случай, когда комплект
собирается, и цвета нужны. Моно-серии (цветов C/M/Y в группе нет вовсе) правило не трогает.
"""
import re
import unicodedata

# Причина -> (человеческое пояснение, идёт ли строка в список наблюдения)
NOVELTY_REASONS = {
    "bulk_toner": ("тонер больше 150 г (берём только разовую заправку)", False),
    "bulk_ink": ("чернила больше 150 мл (берём только разовую заправку)", False),
    "cleaning": ("промывочная жидкость — не наш товар", False),
    "refillable": ("картридж перезаправляемый — не наш товар", False),
    "set_incomplete": ("неполный цветовой комплект — ждём остальные цвета", True),
}

REFILL_LIMIT = 150          # г для тонера, мл для чернил — «на одну заправку»
UNIT_TO_BASE = {"мл": 1, "л": 1000, "г": 1, "гр": 1, "кг": 1000}

VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мл|гр|кг|л|г)(?![а-яё])", re.I)
CLEANING_RE = re.compile(r"промывочн|жидкость\s+промывочная|очищающ", re.I)
REFILLABLE_RE = re.compile(r"перезаправляем", re.I)
TONER_RE = re.compile(r"^тонер\b(?!\s*-?\s*картридж)", re.I)
INK_RE = re.compile(r"^чернила\b", re.I)
COMPAT_RE = re.compile(r"\bдля\s+(.+)$", re.I)
VENDOR_RE = re.compile(r"^[A-Za-zА-Яа-яЁё]+")

# Цвет в названии. Порядок важен: «чёрный матовый» ловим раньше «чёрного».
COLORS = [
    ("BKM", re.compile(r"чер\w*\s+матов", re.I)),
    ("BK", re.compile(r"\bчер[нh]\w*", re.I)),
    ("C", re.compile(r"\bголуб\w*", re.I)),
    ("M", re.compile(r"\bпурпурн\w*", re.I)),
    ("Y", re.compile(r"\bжелт\w*|\bжёлт\w*", re.I)),
]
FULL_SET = {"BK", "C", "M", "Y"}


def _norm(text):
    """ё -> е, схлопнутые пробелы, нижний регистр: поставщик пишет как придётся."""
    text = unicodedata.normalize("NFKC", str(text or "")).replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def volume(name):
    """(число в базовых единицах, 'мл'|'г') или (None, None). Берём МАКСИМАЛЬНОЕ найденное.

    В названии может быть и объём, и ресурс, и размер комплекта — но объём в граммах
    или миллилитрах пишут один раз, а максимум страхует от «2х500гр.».
    """
    best, best_unit = None, None
    for match in VOLUME_RE.finditer(str(name or "")):
        unit = match.group(2).lower()
        value = float(match.group(1).replace(",", ".")) * UNIT_TO_BASE[unit]
        base = "мл" if unit in ("мл", "л") else "г"
        if best is None or value > best:
            best, best_unit = value, base
    return best, best_unit


def color(name):
    """Код цвета по названию или None (моно-товары и «цвет не указан»)."""
    text = _norm(name)
    for code, pattern in COLORS:
        if pattern.search(text):
            return code
    return None


def kind(name):
    """Что это по названию: тонер-порошок, чернила, промывка, перезаправляемый, картридж."""
    text = _norm(name)
    if CLEANING_RE.search(text):
        return "cleaning"
    if REFILLABLE_RE.search(text):
        return "refillable"
    if TONER_RE.search(text):
        return "toner"
    if INK_RE.search(text):
        return "ink"
    return "cartridge"


def models(row):
    """Модели принтеров из хвоста «…для Canon iR C3326/C3326i» -> {'canon ir c3326','c3326i'}.

    Точное сравнение хвоста не годится: у одного и того же комплекта цвета описаны
    вразнобой («C3326» у пурпурного, «C3326/C3326i» у остальных) — комплект развалился бы
    на четыре одиночки. Поэтому сравниваем по пересечению моделей. Куски без цифры или
    без буквы («30», «без чипа») выкидываем: они склеили бы чужие серии.
    """
    compat = COMPAT_RE.search(_norm(str(row.get("name") or "")))
    if not compat:
        return set()
    out = set()
    for chunk in re.split(r"[/,;]", compat.group(1)):
        chunk = chunk.strip(" .()")
        if len(chunk) >= 3 and re.search(r"\d", chunk) and re.search(r"[a-zа-я]", chunk):
            out.add(chunk)
    return out or {compat.group(1)}


def namespace(row):
    """Комплект собирается в границах типа товара — и только его.

    Бренд намеренно НЕ учитываем (решение Сергея 06.08): комплект на принтер мы вправе
    собрать из разных производителей, и цвет от Cet достраивает набор Cactus. Тип же
    смешивать нельзя — флакон тонера не закрывает недостающий цвет картриджа.
    """
    return (kind(str(row.get("name") or "")),)


class Sets:
    """Цветовые комплекты прайса: строки склеиваются в серию по общим моделям принтеров."""

    def __init__(self, rows):
        self._parent = {}
        for row in rows:
            self._link(self._keys(row))
        self._colors = {}
        for row in rows:
            code = color(row.get("name"))
            if code:
                self._colors.setdefault(self._root(row), set()).add(code)

    def _keys(self, row):
        ns = namespace(row)
        found = models(row)
        return [(ns, m) for m in found] or [(ns, "#" + _norm(row.get("name")))]

    def _find(self, key):
        self._parent.setdefault(key, key)
        while self._parent[key] != key:
            self._parent[key] = self._parent[self._parent[key]]
            key = self._parent[key]
        return key

    def _link(self, keys):
        first = self._find(keys[0])
        for key in keys[1:]:
            self._parent[self._find(key)] = first

    def _root(self, row):
        return self._find(self._keys(row)[0])

    def colors(self, row):
        """Цвета, которые есть у серии этой строки во всём прайсе поставщика."""
        return self._colors.get(self._root(row), set())


def check(row, sets):
    """Причина отказа для одной строки-новинки или None, если новинка нам подходит."""
    name = str(row.get("name") or "")
    what = kind(name)
    if what == "cleaning":
        return "cleaning"
    if what == "refillable":
        return "refillable"
    if what in ("toner", "ink"):
        value, unit = volume(name)
        want = "г" if what == "toner" else "мл"
        if value is not None and unit == want and value > REFILL_LIMIT:
            return "bulk_toner" if what == "toner" else "bulk_ink"
    own = color(name)
    if own:
        have = sets.colors(row)
        if have & {"C", "M", "Y"} and not FULL_SET <= have:
            return "set_incomplete"
    return None


COLOR_NAMES = {"BK": "чёрный", "C": "голубой", "M": "пурпурный", "Y": "жёлтый"}


def missing_colors(row, sets):
    """Каких цветов комплекту не хватает — словами, для файла наблюдения."""
    have = sets.colors(row)
    return ", ".join(COLOR_NAMES[c] for c in ("BK", "C", "M", "Y") if c not in have)


def screen(skipped, all_rows, reasons=("not_found", "ambiguous")):
    """Проставить причины отсева новинкам. -> (skipped, счётчик по причинам, список наблюдения).

    Строку не выбрасываем: она остаётся в журнале и в отчёте, но с новой причиной,
    поэтому уходит и из витрины `prc_unmatched`, и из файла новинок оператору.
    """
    sets = Sets(all_rows)
    stats, watch = {}, []
    for row in skipped:
        if row.get("reason") not in reasons:
            continue
        reason = check(row, sets)
        if not reason:
            continue
        row["reason"] = reason
        stats[reason] = stats.get(reason, 0) + 1
        if NOVELTY_REASONS[reason][1]:
            row["missing"] = missing_colors(row, sets)
            watch.append(row)
    return skipped, stats, watch
