# поток: prc
# -*- coding: utf-8 -*-
"""
Разбор названия картриджа на признаки: модель, тип, бренд принтера, ресурс, цвет, чип.

Один разбор на два мира: строки прайса поставщика и карточки нашего каталога пишут одно
и то же по-разному («11000 стр.», «11K», «(11k)», «17,5K», «11000 копий»), поэтому
сравнивать названия можно только через признаки, а не текстом.

Модель — не одна строка, а НАБОР кодов: в названии сидят и код расходника (C-EXV65,
TK-8335C), и коды принтеров (iR C3326i), и внутренний код поставщика (CET141426R).
Какой из них модель, заранее не известно, поэтому берём все и сравниваем пересечением;
редкость кода в каталоге сама расставит их по важности.
"""
import re
import unicodedata

COLOR_NAMES = {"BK": "чёрный", "C": "голубой", "M": "пурпурный", "Y": "жёлтый",
               "BKM": "чёрный матовый", "GY": "серый", "LC": "светло-голубой",
               "LM": "светло-пурпурный", "R": "красный", "B": "синий", "G": "зелёный"}

# Короткие обозначения цвета («, C,» / «Bk,») берём только отдельным словом и НЕ внутри кода:
# в «C-EXV65» буква C — часть модели, а не голубой цвет.
SHORT = r"(?<![\w\-])%s(?![\w\-])"

# Порядок важен: составные («чёрный матовый», «light cyan») ловим раньше простых.
COLOR_PATTERNS = [
    ("BKM", r"чер\w*\s+матов|matte\s*black|" + SHORT % "mbk" + "|" + SHORT % "mk"),
    ("LC", r"светло-?голуб\w*|light\s*cyan|" + SHORT % "lc"),
    ("LM", r"светло-?пурпурн\w*|light\s*magenta|" + SHORT % "lm"),
    ("GY", r"\bсер[ыо]\w*|\bgray\b|\bgrey\b|" + SHORT % "gy"),
    # black/чёрный — только отдельным словом: «Hi-Black» и «White Box» это бренды, не цвет.
    # `(?!ил)` — про «Чернила»: слово само начинается с «черн», и без оговорки ЛЮБЫЕ чернила
    # читались как чёрные (голубой CS-I-CL441C лежал в базе с цветом BK).
    ("BK", r"\bчерн(?!ил)\w*|" + SHORT % "black" + "|" + SHORT % "bk" + "|" + SHORT % "blk"),
    ("C", r"\bголуб\w*|\bcyan\b|" + SHORT % "cy" + "|" + SHORT % "c"),
    ("M", r"\bпурп\w*|\bmagenta\b|" + SHORT % "mg" + "|" + SHORT % "ma" + "|" + SHORT % "m"),
    ("Y", r"\bжелт\w*|\byellow\b|" + SHORT % "ye" + "|" + SHORT % "yl" + "|" + SHORT % "y"),
]
COLOR_RE = [(code, re.compile(pat, re.I)) for code, pat in COLOR_PATTERNS]

# Ресурс: «11000 стр.», «11000 копий», «11K», «17,5K», «(11k)», «ч/б:500000стр.».
# Слева от числа не должно быть буквы или дефиса: в «C-EXV65K» это часть кода модели,
# а не «65 тысяч страниц».
# Пробел внутри числа — только разделитель тысяч («23 000 стр.»), поэтому после него ровно
# три цифры. Иначе «HP LJ Pro 4004/4104 9,7K» склеивалось в 41049, а «Bizhub 227/287/367 23K»
# в 36723 — и родня одного и того же товара расходилась по ресурсу на ровном месте.
# Слэш перед числом тоже отсекаем: «/4104 9,7K» — это модель принтера, а не ресурс.
RESOURCE_RE = re.compile(
    r"(?<![A-Za-zА-Яа-я\d\-/])(\d+(?:\s\d{3})*(?:[.,]\d+)?)\s*(?:(k|к)\b|стр|копи|pages|pag)", re.I)

CHIP_PATTERNS = [
    ("chip_free", r"без\s*счет\w*|безлимит|unlimited|no\s*count"),
    ("nochip", r"без\s*чипа|без\s*чип\b|w/?o\s*chip|no\s*chip|чип\s*в\s*комплект\w*\s*нет"),
    ("chip", r"с\s*чипом|\bчип\b|\bchip\b|в\s*компл\w*[.:\s]*чип"),
]
CHIP_RE = [(code, re.compile(pat, re.I)) for code, pat in CHIP_PATTERNS]
CHIP_NAMES = {"chip": "с чипом", "chip_free": "с чипом без счётчика",
              "nochip": "без чипа", None: "не указан"}

# Бренды принтеров — по ним ставится «для» в «Название WB» (`ms_import`) и сравниваются
# строка прайса с нашей карточкой. Список закрытый и совпадает с папками каталога МС;
# длинные раньше коротких, чтобы «Konica Minolta» не съелось «Konica».
BRANDS = (
    "Konica Minolta", "Kyocera Mita", "Primera Bravo", "Triumph-Adler", "Katusha",
    "Avision", "Brother", "Canon", "Dell", "Deli", "Develop", "Epson", "Gestetner",
    "Huawei", "Konica", "Kyocera", "Lexmark", "Minolta", "Nashuatec", "Olivetti", "Oki",
    "Panasonic", "Pantum", "Primera", "Ricoh", "Samsung", "Sharp", "Sindoh", "Toshiba",
    "Utax", "Xerox", "HP", "F+ imaging", "F+", "Катюша",
)
# re.escape — из-за «F+»: плюс в регулярке это повтор, без экранирования шаблон ломался.
BRAND_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(" + "|".join(re.escape(b) for b in BRANDS) +
                      r")(?![0-9A-Za-zА-Яа-я])", re.IGNORECASE)

# Один и тот же производитель зовётся по-разному: поставщик пишет «Kyocera Mita», мы —
# «Kyocera»; Develop и Minolta — торговые имена той же Konica Minolta; Utax — марка
# Triumph-Adler. Без сведения к одному имени 8 строк из 47 давали ложный конфликт бренда.
BRAND_CANON = {
    "kyocera mita": "Kyocera", "kyocera": "Kyocera",
    "konica minolta": "Konica Minolta", "konica": "Konica Minolta",
    "minolta": "Konica Minolta", "develop": "Konica Minolta",
    "utax": "Triumph-Adler", "triumph-adler": "Triumph-Adler",
    "katusha": "Катюша", "катюша": "Катюша",
    "primera bravo": "Primera", "primera": "Primera",
}

# Код модели: буквы+цифры, минимум 4 знака. «11000» и «CANON» — не коды.
CODE_RE = re.compile(r"[A-ZА-Я0-9][A-ZА-Я0-9\-]{2,}", re.I)
COLOR_SUFFIX_RE = re.compile(r"(BK|BLK|MBK|MK|CY|MG|YE|YL|LC|LM|GY|[CMYKB])$", re.I)
NOISE_CODES = {"MFP", "MPS", "A4", "A3"}


def norm(text):
    """ё→е, NFKC, схлопнутые пробелы, нижний регистр."""
    text = unicodedata.normalize("NFKC", str(text or "")).replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def color(name):
    """Код цвета или None. Однобуквенные обозначения («, C,») ловим только отдельным словом."""
    text = norm(name)
    for code, pattern in COLOR_RE:
        if pattern.search(text):
            return code
    return None


def resource(name):
    """Ресурс печати в страницах или None.

    Берём МАКСИМАЛЬНОЕ из найденного: в названии рядом с ресурсом попадаются объём тонера
    и число цветов, но именно ресурс — самое большое число со «стр./копий/K».
    """
    best = None
    for match in RESOURCE_RE.finditer(str(name or "")):
        raw = match.group(1).replace(" ", "").replace(",", ".")
        # Ведущий ноль бывает только в коде, не в ресурсе: «7Q 069K для Canon» — это
        # картридж 069, а не 69 000 страниц (реальный ресурс 2100 стоял рядом и проигрывал).
        if re.match(r"0\d", raw):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # «17,5K» и «11K» = тысячи. Но «11000к» — это сокращённое «копий», а не 11 миллионов:
        # множитель применяем только к числу, которое без него ресурсом быть не может.
        if match.group(2) and value < 1000:
            value *= 1000
        if value >= 100 and (best is None or value > best):
            best = value
    return int(best) if best else None


def chip(name):
    """'chip' | 'chip_free' | 'nochip' | None (в названии про чип ничего)."""
    text = norm(name)
    for code, pattern in CHIP_RE:
        if pattern.search(text):
            return code
    return None


def _add(out, code):
    """Код и его версия без цвета в хвосте — если это вообще похоже на код."""
    if len(code) < 4 or code in NOISE_CODES:
        return
    if not (re.search(r"\d", code) and re.search(r"[A-Z]", code)):
        return          # «11000» и «CANON» — не коды; кириллица в коде тоже («11000К» = копий)
    out.add(code)
    stripped = COLOR_SUFFIX_RE.sub("", code)
    if stripped != code:
        _add(out, stripped)


def codes(*texts):
    """Все коды из названия и артикула — со снятыми префиксами поставщика и хвостом цвета.

    Один и тот же расходник поставщики пишут по-своему: `GP-C-EXV65C`, `CS-C-EXV65C`,
    `HB-C-EXV65C` — общий у них только хвост. Поэтому от каждого кода берём ВСЕ его хвосты
    по дефисам (`GPCEXV65C`, `CEXV65C`, `EXV65C`): лишние варианты никому не мешают, а нужный
    гарантированно найдётся. Цвет в хвосте снимаем — у поставщика он в коде (`TK-8335C`),
    у нас может быть отдельным полем.
    """
    out = set()
    for text in texts:
        for raw in CODE_RE.findall(str(text or "")):
            parts = [p for p in raw.upper().split("-") if p]
            for start in range(len(parts)):
                _add(out, re.sub(r"[^A-ZА-Я0-9]", "", "".join(parts[start:])))
    return out


def brand(name):
    """МНОЖЕСТВО брендов принтеров из названия, сведённых к каноническому имени.

    Именно множество, а не строка: в строке прайса законно стоят два бренда сразу —
    один и тот же картридж подходит и к HP, и к Canon («CF226/Canon 052H»). Сравнение
    двух названий = непустое пересечение множеств, как у кодов модели.

    Бренд ПОСТАВЩИКА (Cactus, Hi-Black, SuperFine) в список не входит и сюда не попадает.
    """
    out = set()
    for match in BRAND_RE.finditer(str(name or "")):
        found = match.group(1)
        out.add(BRAND_CANON.get(found.lower(), found))
    return out


def brand_text(brands):
    """Множество брендов -> строка для показа человеку ('' — в названии бренда нет)."""
    return ", ".join(sorted(brands or ()))


def parse(name, article=None):
    """Название (и артикул) -> признаки для сравнения."""
    return {"color": color(name), "resource": resource(name), "chip": chip(name),
            "codes": codes(name, article), "brand": brand(name)}
