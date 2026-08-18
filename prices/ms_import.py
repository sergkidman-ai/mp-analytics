# поток: prc
# -*- coding: utf-8 -*-
"""
Файл импорта новых карточек в МойСклад по строкам, сопоставленным с нашим каталогом.

Главное правило: новая карточка — брат-близнец тех, что уже заведены с тем же ВНЕШНИМ КОДОМ.
Всё, что описывает ТОВАР (группа, вес, штрихкод Code128, «Название WB»), берём у родни;
всё, что описывает ПОСТАВКУ (наименование, артикул, цена, поставщик), — из прайса.
Ничего не сочиняем: если у родни поле пустое, строка уходит в отчёт на ручное заполнение,
а не заполняется догадкой.

Что подтверждено фактом МС (инструкция «Новинки в прайсах поставщиков» местами устарела):
  • Код = внешний код + аббревиатура. У Феррета в базе почти всё под «cs» (Cactus 3049,
    G&G 729 из 731, Print-Rite 57, Cet 44) — но у картриджей CET аббревиатура «ce»
    (решение Сергея 08.08): в базе там просто накопилась ошибка человека.
  • Штрихкод Code128 = DS + 3 буквы + внешний код с ведущими нулями и ОДИН И ТОТ ЖЕ у всех
    карточек внешнего кода (у 1191 — DSNXV0001191 на 28 карточках). ean8 МС генерирует сам,
    его в файл не пишем.
  • «Название WB» тоже общее на внешний код: это название модели, а не поставщика. Шаблон —
    тип + модель + «для» + бренд принтера + цвет + XL ресурс + чип. Предлог «для» в каталоге
    стоит лишь у 7% значений, но это ошибки людей: по решению Сергея (08.08) «для» ставим
    ВСЕГДА перед брендом принтера, даже если у родни его нет.
  • НДС 22 (34645 карточек против 20 у 2069), единица «шт», гарантия 365 — как в инструкции.

Источник веса — прайс поставщика (`supplier_dims`, реальные замеры производителя), и только
если его там нет — вес родни по внешнему коду. Ресурс, отличный от родни, — это норма
(решение Сергея 08.08): пишем в замечания как справку, из файла строку не убираем.

Импорт в МС делает человек: МС → Товары → Импорт → импорт из excel, «Искать = по Артикулу».
Отсюда ничего в МС не пишется.
"""
import argparse
import re
from collections import Counter
from datetime import date, datetime, timedelta
from uuid import UUID
from pathlib import Path

from core.db import execute, query
from .features import BRANDS, BRAND_RE       # noqa: F401 — BRANDS в модуле ждут по имени
from .profiles import get_profile

# Порядок колонок — как в файле загрузки от 30.07 («МС шаблон для новинок»).
COLUMNS = [
    "Группы", "Код", "Внешний код", "Наименование", "Описание", "Артикул",
    "Доп. поле: Код поставщика", "Единица измерения", "Закупочная цена", "НДС",
    "Поставщик", "Вес", "Страна", "Доп. поле: Гарантия/ Срок службы",
    "Доп. поле: Название WB", "Штрихкод Code128", "Доп. поле: Связь",
]

UOM = "шт"
VAT = 22
WARRANTY = 365
COUNTRY = "Китай"

# Аббревиатура кода по артикулу поставщика: (регулярка, аббревиатура). Первое совпадение.
# Пустая регулярка в конце — значение по умолчанию для поставщика.
CODE_SUFFIX = {
    "kaktus_msk": [(r"^CET", "ce"), (r"^CSP-", "csp"), (r"", "cs")],
    # Одиссей везёт несколько брендов, и аббревиатура идёт по бренду, а не по поставщику
    # (решение Сергея 12.08.2026: Аквамарин — `at`, WB — `wb`). «ТУ» — по единственной живой
    # карточке этого формата: `0242ty` / «ТУ TK-350». Значения по умолчанию НЕТ намеренно:
    # незнакомый префикс — это новый бренд, и код ему выбирает человек, а не регулярка.
    "odissey": [(r"^AQ\b", "at"), (r"^WB\b", "wb"), (r"^ТУ\b", "ty")],
    # У Сакуры и Колортека бренд один, аббревиатура в базе одна: `sk` у 1375 из 1384
    # позиций последней загрузки Сакуры, `ct` у 1086 из 1091 у Колортека.
    "sakura": [(r"", "sk")],
    "colortek": [(r"", "ct")],
}
# Контрагент для колонки «Поставщик» — как он назван в МС.
MS_SUPPLIER = {
    "kaktus_msk": 'ООО "КОМПАНИЯ ФЕРРЕТ"',
    "odissey": 'ООО "ОДИССЕЙ"',        # есть ещё карточка 'ООО "ОДИССЕЙ" WB' — см. SUPPLIER_BY_SUFFIX
    "sakura": 'ООО "ПОЗИТИВ"',
    "colortek": 'ООО "КОЛОРТЕК РУС"',
}
# У поставщика бывает несколько юрлиц, и карточки висят на РАЗНЫХ: у Одиссея на «ООО ОДИССЕЙ WB»
# — бренд White Box и ВСЯ СТРУЙКА любого бренда (см. `supplier_of`), на «ООО ОДИССЕЙ» — остальная
# брендованная лазерка (`at` Аквамарин, `hb` Hi-Black). Ставим то же юрлицо, что у родни, иначе карточка окажется
# «чужой» для своего же прайса. Ключ — аббревиатура кода; чего нет здесь — `supplier_ids[0]`.
SUPPLIER_BY_SUFFIX = {
    "odissey": {"wb": ("61d01266-2fe1-11f0-0a80-0f7f000bf967", 'ООО "ОДИССЕЙ" WB')},
}
# White Box — «белая», небрендированная коробка. Решение Сергея 18.08.2026: этот бренд
# отделяем от остального ассортимента Одиссея и вешаем на юрлицо «ООО ОДИССЕЙ WB».
# Признак берём ИЗ НАИМЕНОВАНИЯ — так отличает человек и так написано в прайсе
# («… 3K White Box»). Префикс артикула «WB CF259A» — подстраховка: в живых карточках
# нашлись имена без метки («WB MLT-D203U») и с опечаткой («Wite Box»).
WHITE_BOX_NAME = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(?:WB|Wh?ite\s?Box)(?![0-9A-Za-zА-Яа-я])",
                            re.IGNORECASE)
WHITE_BOX_ARTICLE = re.compile(r"^\s*WB\b", re.IGNORECASE)


def is_white_box(name, article=""):
    """True, если товар Одиссея — бренда White Box (см. SUPPLIER_BY_SUFFIX)."""
    return bool(WHITE_BOX_NAME.search(name or "")) or bool(WHITE_BOX_ARTICLE.match(article or ""))


# Струйка Одиссея живёт на том же юрлице «ООО ОДИССЕЙ WB», что и White Box (решение Сергея
# 18.08.2026 — им же отменён обратный переезд 461 струйной карточки). Признак берём из
# КАТЕГОРИИ МС: «Картриджи/Картриджи Epson/струйные epson», «…/струйные hp», «…/струйные canon».
# Категория — родной признак справочника и надёжнее разбора наименования: по имени струйник
# отличается только кодом модели (T0801, CL-41, PGI-425), а список кодов бесконечен.
INKJET_PATH = re.compile(r"струйн", re.IGNORECASE)


def is_inkjet(path):
    """True, если карточка лежит в струйной категории МС (см. INKJET_PATH)."""
    return bool(INKJET_PATH.search(path or ""))
# Чей прайс в `supplier_dims` — первоисточник веса для этого поставщика.
DIMS_SUPPLIER = {
    "kaktus_msk": ("cactus",),
}

# Бренды принтеров (BRANDS/BRAND_RE) живут в `features` — там же, где ими сравнивают
# строку прайса с нашей карточкой. Здесь по ним ставится «для» в «Название WB».

# Ресурс в названии: «(9000стр.)», «9200 стр.», «23600 копий», «69K». Цифры не должны быть
# продолжением кода модели («006R01828» — это не 1828 страниц), поэтому слева граница.
PAGES_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(\d[\d ]{1,8}?)\s*(?:стр|копий)", re.IGNORECASE)
PAGES_K_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(\d{1,3})\s*[kK]\b")

# Папки-исключения при выборе группы. Карточка живёт в папке БРЕНДА ПРИНТЕРА
# («Картриджи/Картриджи Kyocera Mita»), а не бренда поставщика: папки G&G и BULAT
# завёл когда-то один поставщик, и родня иногда лежит там. Оригиналы — чужой ассортимент.
GROUP_SUPPLIER = ("Картриджи/Картриджи G&G", "Картриджи/Картриджи BULAT")
GROUP_PREFIX = "Картриджи/"


# Аббревиатура кода идёт по БРЕНДУ, а бренд зашит в НАЗВАНИЕ товара (правило Сергея 18.08.2026:
# «человек смотрит на название товара — в нём бренд, по бренду назначается аббревиатура»).
# Порядок важен: «БУЛАТ s-Line» — это s-Line (`sl`), а не Булат (`bt`); голый «БУЛАТ» — `bt`.
# Проверено на 17 380 живых карточках с нашим кодом: правило совпало с фактическим кодом в 98.9 %.
# 7Q ВКЛЮЧЁН 18.08.2026 (решение Сергея) — но только позиционно, см. комментарий у правила.
# Чего нет в списке — уходит человеку.
BRAND_ABBR = [
    ("sl", r"s-?line"),          # 1137 из 1158
    ("el", r"e-?line"),          # 224 из 233
    ("bt", r"булат|bulat"),      # 231 из 240 (после s-Line/e-Line)
    # 7Q — бренд, ТОЛЬКО когда стоит в начале названия (1-3-е слово): «Тонер-картридж 7Q
    # CF226X…». Глубже в названии это упоминание ЧИПА («с чипом 7Q», «без чипа 7Q»)
    # и бренд там другой — такие уходят правилу поставщика. Живьём: начало → 521 карточка
    # с `7q` против 27 с `bt` (старые опечатки), глубже → 33 `bt` не трогаем.
    ("7q", r"^(?:\S+\s+){0,2}7Q\b"),   # 521 из 548 (после Булата: у него 7Q — чип)
    ("bs", r"blossom"),          # 2355 из 2355
    ("pl", r"profiline"),        # 1435 из 1437
    ("gp", r"galaprint"),        # 1134 из 1142
    ("sf", r"superfine"),        # 1732 из 1733
    ("t2", r"\bt2\b"),           # 1032 из 1035
    ("ep", r"easy\s?print"),     # 860 из 861
    ("hb", r"hi-?black"),        # 1219 из 1221
    ("np", r"net\s?product"),    # 399 из 401
    ("cs", r"cactus"),           # 2988 из 3032
    ("sk", r"sakura"),           # 1836 из 1850
]
BRAND_ABBR = [(abbr, re.compile(rx, re.IGNORECASE)) for abbr, rx in BRAND_ABBR]


def abbr_by_name(name):
    """Аббревиатура по бренду из названия товара или None (бренда нет — решает человек)."""
    for abbr, rx in BRAND_ABBR:
        if rx.search(name or ""):
            return abbr
    return None


def suffix(article, supplier_key, name=""):
    """Аббревиатура поставщика для колонки «Код».

    У Одиссея White Box опознаётся по НАИМЕНОВАНИЮ (`is_white_box`), а не только по префиксу
    артикула: в прайсе метка стоит в названии, и артикул её иногда не несёт.
    """
    if supplier_key == "odissey" and is_white_box(name, article):
        return "wb"
    for pattern, abbr in CODE_SUFFIX.get(supplier_key, []):
        if not pattern or re.search(pattern, article or "", re.IGNORECASE):
            return abbr
    raise KeyError(f"нет правила аббревиатуры кода для поставщика '{supplier_key}', "
                   f"артикул «{article}» — добавьте бренд в CODE_SUFFIX")


def supplier_of(article, profile, name="", path=""):
    """(id контрагента, имя) для новой карточки: юрлицо той же группы, что у родни бренда.

    У Одиссея на «ООО ОДИССЕЙ WB» сидят ДВА набора: бренд White Box (метка в наименовании)
    и вся струйка (категория МС, `is_inkjet`) — независимо от бренда. Аббревиатуру кода это
    не трогает: струйный Аквамарин остаётся `…at`, меняется только юрлицо.
    """
    if profile.key == "odissey" and (is_white_box(name, article) or is_inkjet(path)):
        return SUPPLIER_BY_SUFFIX["odissey"]["wb"]
    abbr = suffix(article, profile.key, name)
    found = SUPPLIER_BY_SUFFIX.get(profile.key, {}).get(abbr)
    if found:
        return found
    return profile.supplier_ids[0], MS_SUPPLIER.get(profile.key, profile.title)


def _attrs(payload):
    return {a.get("name"): a.get("value") for a in (payload.get("attributes") or [])}


def wb_name(value):
    """«Название WB» родни, приведённое к шаблону: «для» перед брендом принтера.

    В каталоге предлог стоит редко и местами слипся с моделью («106R03766для Xerox») —
    это ошибки заполнения, а не второй формат. Возвращает (название, замечание).
    Бренд не распознан — ничего не выдумываем, отдаём как есть с пометкой.
    """
    text = re.sub(r"\s+", " ", (value or "").replace(" ", " ")).strip()
    if not text:
        return "", None
    text = re.sub(r"(?<=[0-9A-Za-zА-Яа-я])(для)\b", r" \1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдля\b\s*", "для ", text, flags=re.IGNORECASE)
    match = BRAND_RE.search(text)
    if not match:
        return text, f"бренд принтера в «{text}» не распознан — «для» не поставлено"
    before = text[:match.start()].rstrip()
    if before.lower().endswith("для"):
        return text, None
    return f"{before} для {text[match.start():]}".strip(), None


# Цвет каталога ТК → слово в «Название WB». Смеси и редкие обозначения (BKM, PK…) сюда
# намеренно не включены: цвет там неоднозначен, а выдумывать слово нельзя — пропускаем.
TC_COLOR_WORD = {"BK": "черный", "C": "голубой", "M": "пурпурный", "Y": "желтый",
                 "GY": "серый", "LC": "светло-голубой", "LM": "светло-пурпурный",
                 "MK": "матовый черный"}
# «с чипом без счётчика» — это тоже С чипом (см. CHIP_MAP в collectors/thecartridge_catalog).
TC_CHIP_WORD = {"chip": "с чипом", "chip_free": "с чипом", "nochip": "без чипа"}
XL_RE = re.compile(r"\bxl\b|увеличен", re.IGNORECASE)


def wb_compose(external_code, price_name):
    """«Название WB» по формуле, когда у родни его нет. Возвращает (название, замечание).

    Формула (шаблон Сергея): **тип + модель + «для» + бренд принтера + цвет + чип + XL ресурс**.
    Признаки берём из каталога ТК (`prc_tc_model`) — это первоисточник по ТОВАРУ, а не разбор
    чужих названий: поле в МС заполняли руками и с ошибками (предлог «для» стоит у 7% значений,
    аббревиатура поставщика заезжает в модель, чип пишут через раз), и повторять их нельзя.
    XL — из названия прайса: это признак ПОСТАВКИ, в каталоге ТК его нет.

    Каталог кода не знает или молчит о типе/модели/бренде — ничего не сочиняем, отдаём пусто
    с замечанием: поле заполнит человек.
    """
    rows = query("""SELECT title, color, chip, brand, consumable_type
                      FROM prc_tc_model WHERE external_code = %s AND gone_at IS NULL""",
                 (external_code,))
    if not rows:
        return "", f"каталога ТК по коду {external_code} нет — «Название WB» заполнить вручную"
    tc = rows[0]
    kind = (tc["consumable_type"] or "").strip()
    model = (tc["title"] or "").strip()
    brand = next((b for b in (tc["brand"] or []) if b), None)
    missing = [name for name, value in (("тип", kind), ("модель", model), ("бренд", brand))
               if not value]
    if missing:
        return "", (f"в каталоге ТК по коду {external_code} нет {', '.join(missing)} — "
                    f"«Название WB» заполнить вручную")
    parts = [kind, model, "для", brand]
    parts += [w for w in (TC_COLOR_WORD.get((tc["color"] or "").upper()),
                          TC_CHIP_WORD.get(tc["chip"])) if w]
    if XL_RE.search(price_name or ""):
        parts.append("XL ресурс")
    return " ".join(parts), None


UUID_EPOCH = datetime(1582, 10, 15)


def created_at(ms_id):
    """Когда карточка ЗАВЕДЕНА. Поля создания в API МС нет, но id — время-зависимый UUID:
    в нём лежит счётчик 100-нс от 1582-10-15 (сверено: карточка 08.08 → 08.08, документ
    оприходования 06.08 13:45 → 06.08 13:45). Не разобралось — считаем самой старой.
    """
    try:
        return UUID_EPOCH + timedelta(microseconds=UUID(str(ms_id)).time // 10)
    except (ValueError, AttributeError):
        return UUID_EPOCH


def family(codes):
    """Родня по внешнему коду: живые карточки МС со всем, что нужно новой карточке."""
    rows = query(
        """SELECT p.external_code ec, p.ms_id, p.code, p.article, p.name, p.updated_at,
                  r.payload payload
             FROM ms_product p JOIN raw_moysklad_product r ON r.ms_id = p.ms_id
            WHERE p.external_code = ANY(%s) AND NOT p.archived""",
        (list(codes),))
    out = {}
    for row in rows:
        payload, attrs = row["payload"], _attrs(row["payload"])
        code128 = next((b["code128"] for b in (payload.get("barcodes") or []) if b.get("code128")), None)
        weight = payload.get("weight")
        wb, wb_note = wb_name(attrs.get("Название WB"))
        out.setdefault(row["ec"], []).append({
            "code": row["code"], "article": row["article"], "name": row["name"],
            "updated": row["updated_at"], "created": created_at(row["ms_id"]),
            "path": payload.get("pathName"),
            "weight": float(weight) if weight else None, "code128": code128,
            "wb": wb or None, "wb_note": wb_note,
        })
    return out


# Префикс поставщика в артикуле: «CS-KMTNP80C» и «CR-KMTNP80C» — один и тот же товар
# у двух брендов одного завода. Отрезаем только префикс с разделителем, чтобы не съесть
# кусок кода модели («SACF226X» так и остаётся целым).
SUPPLIER_PREFIX = re.compile(
    r"^(?:CSP|CS|CR|GG|PR|GP|PL|HB|NP|NV|SF|OEM|TC|LX|SA|BS|BT|SL|EL|EP|T2|UT|UJ|UN|MY|IT|ML|ATM|SP|ST|AT|WB|SK)"
    r"[-_ ]", re.IGNORECASE)
_DIMS_CACHE = None


def _core(article):
    """Код модели без префикса поставщика и разделителей: CS-KMTNP80C -> KMTNP80C."""
    return re.sub(r"[^0-9A-Z]", "", SUPPLIER_PREFIX.sub("", str(article or "").upper()))


def weight_from_dims(articles, only=None):
    """Вес из прайсов поставщиков (`supplier_dims`) по кодам моделей.

    Это РЕАЛЬНЫЙ вес производителя, а не расчёт: тот же товар продаётся под кодами разных
    брендов одного завода (CS-KMTNP80C = CR-KMTNP80C). При расхождении больше 20% ничего
    не выбираем — пусть человек посмотрит. `only` ограничивает круг прайсов.
    """
    global _DIMS_CACHE
    cores = {c for c in (_core(a) for a in articles) if len(c) >= 6}
    if not cores:
        return None, None
    if _DIMS_CACHE is None:
        _DIMS_CACHE = [(r["supplier"], r["article"], float(r["weight_kg"]), _core(r["article"]))
                       for r in query("SELECT supplier, article, weight_kg FROM supplier_dims "
                                      "WHERE weight_kg IS NOT NULL AND weight_kg > 0")]
    hits = [h for h in _DIMS_CACHE if h[3] in cores and (only is None or h[0] in only)]
    if not hits:
        return None, None
    values = [h[2] for h in hits]
    if max(values) > min(values) * 1.2:
        return None, f"вес в прайсах поставщиков расходится: {sorted(set(values))} — проверить"
    best = max(hits, key=lambda h: h[2])
    return best[2], f"прайс {best[0]}, арт. {best[1]}"


def _pick(cards, field, keep=None, tie=None):
    """Значение поля у родни: что чаще; при равенстве — правило tie, иначе свежая карточка.

    keep отсеивает заведомо неподходящие варианты (чужая папка, кривое написание), но
    только если после отсева что-то осталось. Возвращает (значение, [все варианты] —
    в отчёт: расхождение внутри внешнего кода это вопрос к человеку, а не повод молчать).
    """
    values = [c[field] for c in cards if c.get(field) not in (None, "", 0)]
    if not values:
        return None, []
    counts = Counter(values)
    pool = [v for v in counts if keep(v)] if keep else list(counts)
    pool = pool or list(counts)
    top = max(counts[v] for v in pool)
    best = [v for v in pool if counts[v] == top]
    if len(best) == 1:
        chosen = best[0]
    elif tie:
        chosen = sorted(best, key=tie, reverse=True)[0]
    else:
        fresh = sorted((c for c in cards if c.get(field) in best),
                       key=lambda c: c["updated"] or date.min, reverse=True)
        chosen = fresh[0][field]
    return chosen, [v for v, _ in counts.most_common()]


# Цвет в названии прайса → как он пишется в «Название WB».
COLOR_MARKS = {
    "черн": ("черн", "black", "чёрн"),
    "голуб": ("голуб", "cyan"),
    "пурпур": ("пурпур", "magenta"),
    "желт": ("желт", "yellow", "жёлт"),
}


def _wb_fit(price_name):
    """Сравнение вариантов «Название WB» с формулой: тип + модель + «для» + бренд принтера
    + цвет + XL ресурс + чип. Цвет, XL и чип сверяем с названием из прайса — берём тот
    вариант родни, который формуле соответствует, а не просто самый длинный. Предлог «для»
    уже проставлен (см. `wb_name`), здесь остаётся отсеять аббревиатуру поставщика,
    заехавшую в модель («Картридж SP TN-221C»).
    """
    low = (price_name or "").lower()
    color = next((ru for ru, marks in COLOR_MARKS.items() if any(m in low for m in marks)), None)
    xl = bool(re.search(r"\bxl\b|увеличен", low))
    chip = "чип" in low

    def score(value):
        v = value.lower().replace("ё", "е")
        return (1 if color and color in v else 0,
                1 if xl and "xl" in v else 0,
                1 if chip and "чип" in v else 0,
                0 if re.match(r"^\S+\s+(SP|CS|GG|NV|PL|GP|T2|HB)\s", value) else 1,
                len(value))
    return score


def _pages(text):
    """Ресурс из названия. Меньше сотни страниц картриджей не бывает — такое отсеиваем."""
    out = {int(re.sub(r"\s+", "", raw)) for raw in PAGES_RE.findall(text or "")}
    out |= {int(raw) * 1000 for raw in PAGES_K_RE.findall(text or "")}
    return {v for v in out if 100 <= v <= 2_000_000}


def build(supplier_key, decisions=("matched",), limit=None, ids=None):
    """Строки файла импорта + замечания по каждой строке.

    `ids` — только эти строки новинок (кнопка «Это он» заводит карточку по одной строке;
    решение к этому моменту уже записано, поэтому фильтр по нему остаётся общим).
    """
    profile = get_profile(supplier_key)
    rows = query(
        """SELECT id, article, name, ms_code, price_rub, link
             FROM prc_novelty
            WHERE supplier_key = %s AND decision = ANY(%s) AND ms_code IS NOT NULL
              AND (%s::int[] IS NULL OR id = ANY(%s::int[]))
            ORDER BY id""",
        (profile.key, list(decisions), list(ids) if ids else None,
         list(ids) if ids else None))
    if limit:
        rows = rows[:limit]
    kin = family({(r["ms_code"] or "")[:4] for r in rows})
    known = {r["article"].strip().upper() for r in query(
        "SELECT article FROM ms_product WHERE article IS NOT NULL AND NOT archived")}

    records, notes = [], []
    for row in rows:
        ext = (row["ms_code"] or "")[:4]
        cards = kin.get(ext) or []
        flags = []
        if not cards:
            notes.append((row["id"], row["article"], ["нет живой родни по внешнему коду — проверить код"]))
            continue
        path, path_all = _pick(
            cards, "path",
            keep=lambda p: p.startswith(GROUP_PREFIX) and not p.startswith(GROUP_SUPPLIER))
        code128, bc_all = _pick(cards, "code128")
        wb, wb_all = _pick(cards, "wb", tie=_wb_fit(row["name"]))

        # Вес у родни расходится — берём САМУЮ СВЕЖУЮ карточку внешнего кода (решение
        # Сергея 08.08): её заводили последней, по последним данным.
        weighted = sorted((c for c in cards if c.get("weight")),
                          key=lambda c: c["created"], reverse=True)
        kin_weight = weighted[0]["weight"] if weighted else None
        weight_all = sorted({c["weight"] for c in weighted})

        # Первоисточник веса — прайс САМОГО поставщика по артикулу из прайса; дальше вес
        # родни в МС; в последнюю очередь — прайсы других поставщиков по кодам родни.
        weight, source = weight_from_dims([row["article"]], only=DIMS_SUPPLIER.get(profile.key))
        if weight and kin_weight and abs(weight - kin_weight) > 0.2 * max(weight, kin_weight):
            flags.append(f"вес поставщика {weight} против {kin_weight} у родни — взял поставщика ({source})")
        if not weight and kin_weight:
            weight = kin_weight
            source = (f"карточка {weighted[0]['code']}, заведена "
                      f"{weighted[0]['created']:%d.%m.%Y} — самая свежая по внешнему коду")
        if not weight:
            weight, source = weight_from_dims([c["article"] for c in cards])
        if not weight:
            flags.append(source or "веса нет ни в прайсах поставщиков, ни у родни — заполнить по nix.ru")

        if len(path_all) > 1:
            flags.append(f"группа у родни разная: {' / '.join(path_all)} → взял «{path}»")
        if len(bc_all) > 1:
            flags.append(f"штрихкод у родни разный: {' / '.join(bc_all)} → взял {code128}")
        if not code128:
            flags.append("у родни нет Code128 — заполнить вручную")
        if len(wb_all) > 1:
            flags.append(f"«Название WB» у родни разное: {' / '.join(wb_all)} → взял «{wb}»")
        if not wb:
            # У родни поля нет — собираем по формуле из каталога ТК, а не оставляем пустым:
            # с пустым «Название WB» карточка уезжает на WB безымянной (решение Сергея 14.08).
            wb, wb_flag = wb_compose(ext, row["name"])
            flags.append(f"«Название WB» у родни нет — собрано по формуле из каталога ТК: «{wb}»"
                         if wb else wb_flag)
        flags += sorted({c["wb_note"] for c in cards if c.get("wb_note")})
        if len(weight_all) > 1 and max(weight_all) > min(weight_all) * 1.2:
            flags.append(f"вес у родни расходится: {weight_all}, в файле {weight} — {source}")
        if row["price_rub"] is None:
            flags.append("нет цены в прайсе")
        if row["article"].strip().upper() in known:
            flags.append("артикул уже есть в МС — импорт по артикулу ПЕРЕПИШЕТ ту карточку")
        if row["link"]:
            flags.append(f"связь {row['link']} проставлена вручную — записал в «Связь»")
        new_pages, kin_pages = _pages(row["name"]), set().union(*(_pages(c["name"]) for c in cards))
        if new_pages and kin_pages and not (new_pages & kin_pages):
            flags.append(f"ресурс отличается от родни: {sorted(new_pages)} против {sorted(kin_pages)} "
                         f"— по решению Сергея это норма, оставляем")

        sup_id, sup_name = supplier_of(row["article"], profile, row["name"], path)
        records.append({
            # Служебное поле (не колонка файла): по нему после создания карточки строка
            # новинок получает свой итог. `write_xlsx` идёт по COLUMNS, лишний ключ ей не мешает.
            "_novelty_id": row["id"],
            "Группы": path or "",
            "Код": f"{ext}{suffix(row['article'], profile.key, row['name'])}",
            "Внешний код": ext,
            "Наименование": row["name"],
            "Описание": row["name"],
            "Артикул": row["article"],
            "Доп. поле: Код поставщика": row["article"],
            "Единица измерения": UOM,
            "Закупочная цена": float(row["price_rub"]) if row["price_rub"] is not None else "",
            "НДС": VAT,
            # Служебное: юрлицо-владелец карточки (у поставщика их несколько, см. supplier_of)
            "_supplier_id": sup_id,
            "Поставщик": sup_name,
            "Вес": weight or "",
            "Страна": COUNTRY,
            "Доп. поле: Гарантия/ Срок службы": WARRANTY,
            "Доп. поле: Название WB": wb or "",
            "Штрихкод Code128": code128 or "",
            "Доп. поле: Связь": row["link"] or "",
        })
        if flags:
            notes.append((row["id"], row["article"], flags))
    return records, notes


# --- запись карточек в МС по API -------------------------------------------------------
# Те же значения, что уходят в excel-файл: колонка → (id доп. поля в МС, тип).
ATTRS = {
    "Доп. поле: Код поставщика": ("efaefd7a-b130-11ea-0a80-0367000245f4", "string"),
    "Доп. поле: Гарантия/ Срок службы": ("84ff57f8-536d-11ec-0a80-00200028b27e", "long"),
    "Доп. поле: Название WB": ("46231e52-76f6-11ee-0a80-0282001a60b3", "text"),
    "Доп. поле: Связь": ("e5020d07-243d-11f1-0a80-19050008c201", "string"),
}
UOM_PCS = "19f1edc0-fc42-4001-94cb-c9ec9c62ec10"      # шт
COUNTRY_CN = "fd44cd2e-b398-4222-9c43-f75688bdf327"   # Китай


def _folders():
    """Полный путь папки → id: «Картриджи/Картриджи HP» → uuid."""
    from core import ms_api
    out = {}
    for row in ms_api.iter_rows("/entity/productfolder", page=500):
        path = row.get("pathName")
        out[f"{path}/{row['name']}" if path else row["name"]] = row["id"]
    return out


def next_external_codes(n=1):
    """Следующие свободные 4-значные внешние коды, N штук подряд.

    Максимум берём из нашего слепка `ms_product`, но каждый кандидат ещё и спрашиваем у МС:
    слепок собирается раз в сутки, а карточки за это время заводят и руками.
    """
    from core import ms_api
    last = query("SELECT max(external_code::int) m FROM ms_product "
                 "WHERE external_code ~ '^[0-9]{4}$'")[0]["m"] or 0
    out = []
    while len(out) < n:
        last += 1
        if ms_api.get("/entity/product", {"filter": f"externalCode={last}", "limit": 1}).get("rows"):
            continue
        out.append(str(last))
    return out


def create_in_ms(records, supplier_id, dry=True, log=print, own=None):
    """Создать карточки в МС. Существующий артикул не трогаем — только пропускаем.

    Осознанно НЕ обновляем найденные карточки: перезапись живого товара из прайса —
    отдельное решение, а не побочный эффект заведения новинок.

    `supplier_id` — запасное юрлицо: у записи своё (`_supplier_id`, см. `supplier_of`),
    потому что у поставщика юрлиц несколько и карточки брендов висят на разных.

    `own` — id юрлиц НАШЕЙ группы (`prices/supplier_group.own_ids`). С ним дублем считается
    только карточка своей группы: один и тот же артикул (CET…, OEM-код) бывает заведён и у
    другого поставщика, и та карточка нашему прайсу остатка не даст — оприходование берёт
    товар только своей группы. Карточку с пустым поставщиком тоже считаем дублем: её
    загрузчик берёт (владельца она не называет), и вторая карточка задвоила бы остаток.
    Без `own` поведение прежнее: любой найденный артикул — дубль.
    """
    from core import ms_api
    folders, created, skipped = _folders(), [], []
    for rec in records:
        article = rec["Артикул"]
        # Артикул поставщика ставится только на ГЛАВНУЮ карточку позиции: если один универсальный
        # картридж закрывает несколько наших товаров, у остальных карточек артикула нет —
        # тогда на дубли проверяем по коду (он уникален по определению).
        field, value = ("article", article) if article else ("code", rec["Код"])
        found = ms_api.get("/entity/product",
                           {"filter": f"{field}={value}", "limit": 1 if own is None else 20})
        rows = found.get("rows") or []
        if rows and own is not None and article:
            rows = [c for c in rows
                    if (ms_api.meta_id(c, "supplier") or None) in set(own) | {None}]
        if rows:
            skipped.append((value, f"{'артикул' if article else 'код'} уже есть в МС"))
            continue
        folder = folders.get(rec["Группы"])
        if not folder:
            skipped.append((article, f"нет папки «{rec['Группы']}»"))
            continue
        body = {
            "name": rec["Наименование"], "description": rec["Описание"],
            "code": rec["Код"], "externalCode": rec["Внешний код"],
            "vat": VAT, "vatEnabled": True, "useParentVat": False,
            "uom": ms_api.ref("uom", UOM_PCS),
            "country": ms_api.ref("country", COUNTRY_CN),
            "supplier": ms_api.ref("counterparty", rec.get("_supplier_id") or supplier_id),
            "productFolder": ms_api.ref("productfolder", folder),
            "attributes": [{
                "meta": {"href": f"{ms_api.BASE}/entity/product/metadata/attributes/{ATTRS[col][0]}",
                         "type": "attributemetadata", "mediaType": "application/json"},
                "type": ATTRS[col][1], "value": rec[col],
            } for col in ATTRS if rec.get(col) not in (None, "")],
        }
        if article:                       # пустой артикул в МС не отправляем — не пустое поле, а его отсутствие
            body["article"] = article
        if rec["Вес"]:
            body["weight"] = float(rec["Вес"])
        if rec["Закупочная цена"] != "":
            body["buyPrice"] = {"value": round(float(rec["Закупочная цена"]) * 100, 2)}
        if rec["Штрихкод Code128"]:
            body["barcodes"] = [{"code128": rec["Штрихкод Code128"]}]
        if dry:
            log(f"  [проба] {rec['Код']} {article} — {rec['Наименование'][:60]}")
            continue
        made = ms_api.post("/entity/product", body)
        created.append((made["id"], rec["Код"], article))
        log(f"  создано {rec['Код']} {article} → {made['id']}")
    for article, why in skipped:
        log(f"  пропуск {article}: {why}")
    return created, skipped


def mark_created(records, created):
    """Строке новинок проставить СОЗДАННУЮ карточку и статус «уже в МС».

    До этого в `ms_code` оставался код РОДСТВЕННИКА, по которому человек опознал позицию
    («это он» → `0247gp`), и по вкладке нельзя было понять, доведена ли работа до карточки.
    Теперь там код заведённой карточки (`0247at`), а статус `exists` уводит строку из
    «сопоставленных»: разбирать больше нечего. Родня не теряется — у неё тот же внешний код.
    """
    by_code = {r["Код"]: r for r in records}
    done = 0
    for ms_id, code, _article in created:
        rec = by_code.get(code)
        if not rec or not rec.get("_novelty_id"):
            continue
        execute("""UPDATE prc_novelty
                      SET decision = 'exists', ms_code = %s, ms_id = %s, ms_name = %s,
                          decided_at = now()
                    WHERE id = %s""",
                (code, str(ms_id), rec["Наименование"], rec["_novelty_id"]))
        done += 1
    return done


def mark_existing(records, profile, log=print):
    """Строки, где карточка поставщика УЖЕ была заведена, тоже закрыть как «уже в МС».

    Карточку могли завести руками или прошлым прогоном — тогда `create_in_ms` её пропускает,
    а строка новинок остаётся висеть работой, которой нет. Закрываем только если карточка
    принадлежит группе юрлиц ЭТОГО поставщика: чужая карточка с тем же артикулом —
    не повод считать позицию заведённой (её как раз надо завести своей).
    """
    from core import ms_api
    from .supplier_group import name_of, own_ids
    ours, done, foreign = own_ids(profile), 0, []
    for rec in records:
        if not rec.get("_novelty_id"):
            continue
        field, value = ("article", rec["Артикул"]) if rec["Артикул"] else ("code", rec["Код"])
        found = ms_api.get("/entity/product", {"filter": f"{field}={value}", "limit": 2}).get("rows", [])
        mine = [c for c in found if ms_api.meta_id(c, "supplier") in ours]
        if not mine:
            if found:
                foreign.append((value, name_of(ms_api.meta_id(found[0], "supplier"))))
            continue
        card = mine[0]
        execute("""UPDATE prc_novelty
                      SET decision = 'exists', ms_code = %s, ms_id = %s, ms_name = %s,
                          decided_at = now()
                    WHERE id = %s AND decision <> 'exists'""",
                (card.get("code"), card["id"], card.get("name"), rec["_novelty_id"]))
        done += 1
    for value, who in foreign:
        log(f"  {value}: карточка есть, но она чужая — «{who}»; строку не закрываю")
    return done, foreign


def write_xlsx(records, path):
    from openpyxl import Workbook
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Товары"
    sheet.append(COLUMNS)
    for rec in records:
        sheet.append([rec.get(col, "") for col in COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMNOPQ", (28, 10, 12, 70, 70, 24, 24, 8, 14, 6, 26, 8, 10, 10, 46, 16, 10)):
        sheet.column_dimensions[col].width = width
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_notes(notes, path, total):
    lines = [f"# Файл импорта в МС: что проверить\n",
             f"Строк в файле: {total}. Строк с замечаниями: {len(notes)}.\n"]
    for novelty_id, article, flags in notes:
        lines.append(f"\n## {article} (строка новинок {novelty_id})\n")
        lines += [f"- {f}\n" for f in flags]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="файл импорта новых карточек в МойСклад")
    ap.add_argument("supplier", help="ключ поставщика (kaktus, colortek, …)")
    ap.add_argument("--decision", default="matched", help="какие строки новинок брать")
    ap.add_argument("--limit", type=int, help="взять только первые N строк")
    ap.add_argument("--only", help="коды через запятую: только эти строки (для теста)")
    ap.add_argument("--apply", action="store_true", help="создать карточки в МС (без него — проба)")
    ap.add_argument("--to-ms", action="store_true", help="показать, что уйдёт в МС, ничего не создавая")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs" / "prc"))
    args = ap.parse_args()

    from .supplier_group import own_ids
    profile = get_profile(args.supplier)
    records, notes = build(profile.key, tuple(args.decision.split(",")), args.limit)
    if args.only:
        keep = {c.strip() for c in args.only.split(",")}
        records = [r for r in records if r["Код"] in keep]
    if args.apply or args.to_ms:
        # Дубль — только карточка своей группы: тот же артикул у другого поставщика остатка
        # нашему оприходованию не даст (см. create_in_ms).
        created, skipped = create_in_ms(records, profile.supplier_ids[0], dry=not args.apply,
                                        own=own_ids(profile))
        marked = mark_created(records, created) if args.apply else 0
        was = 0
        if args.apply and skipped:
            # Пропущенные — те, у кого карточка уже есть; строку новинок надо закрыть тоже,
            # иначе она висит работой, которой нет.
            have = {v for v, why in skipped if "уже есть" in why}
            was, _ = mark_existing([r for r in records
                                    if (r["Артикул"] or r["Код"]) in have], profile)
        print(f"создано: {len(created)}; пропущено: {len(skipped)}; строк на входе: {len(records)}"
              + (f"; строк новинок закрыто: {marked + was} (из них уже были в МС: {was})"
                 if args.apply else ""))
        return
    stamp = f"{date.today():%Y-%m-%d}"
    out = Path(args.out)
    xlsx = write_xlsx(records, out / f"{profile.key}_{stamp}_ms_import.xlsx")
    md = write_notes(notes, out / f"{profile.key}_{stamp}_ms_import_notes.md", len(records))
    print(f"строк в файле: {len(records)}; с замечаниями: {len(notes)}")
    print(f"файл импорта: {xlsx}")
    print(f"замечания:    {md}")


if __name__ == "__main__":
    main()
