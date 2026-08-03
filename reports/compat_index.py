# поток: rev
"""reports/compat_index.py — индекс совместимости «модель принтера → наши карточки».

ЗАЧЕМ. Покупатель называет СВОЙ ПРИНТЕР («подойдёт на Brother DCP-7180DN?») или код оригинала,
а мы продаём совместимые картриджи под своими кодами. Прямой ILIKE по названию листинга
(старый путь `catalog._search`) находил только то, что влезло в title — 5–6 моделей из десятков,
и промахивался на суффиксах (`DCP-7180DN` не находил карточку «DCP-7180»).

ЧТО ДЕЛАЕТ. Офлайн-сборка (`--build`) вытаскивает модели принтеров из всего, что у нас есть:
  * `compat` — характеристика WB «Совместимость картриджа», Ozon-атрибуты 4180 (название-аннотация)
               и 11254 (rich-контент, блок «Для принтеров»);
  * `title`  — названия листингов wb_cards / ozon_product / оферов Маркета;
  * `descr`  — описания карточек (WB `payload->>description`, Ozon 4191, Маркет `offer.description`),
               только куски ПОСЛЕ маркеров совместимости («совместим с…», «подходит для…», «для принтеров»);
  * `cache`  — накопленные вердикты `compat_cache` как обратный индекс: `yes` — плюс к подбору,
               `no` — ГАСИТ пару товар×модель (уже проверяли — не подходит).

Модель нормализуется в три поля: серия + числовое ядро + суффикс (`DCP-7180DN` → серия `dcp`,
ядро `dcp7180`, норма `dcp7180dn`), поэтому запрос с суффиксом находит карточку без суффикса.
Тип товара (`item_kind`) различается: toner / ink / drum / kit / ribbon / head / other — чтобы на
вопрос про фотобарабан не подсовывать тонер-картридж.

Индекс лежит в таблице `compat_index` (миграция 063), читается функцией `lookup()` из
`reports/catalog.py`. Ничего никуда не пишет на площадки — только БД.

    ./venv/bin/python -m reports.compat_index --build          # пересобрать индекс
    ./venv/bin/python -m reports.compat_index --stats          # покрытие
    ./venv/bin/python -m reports.compat_index --check "Brother DCP-7180DN" [wb]
"""
import re
import sys
import pathlib

import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

# ──────────────────────────────── нормализация модели ────────────────────────────────

# Атом модели: [серия][-]цифры[суффикс]. Серия приклеена вплотную (через дефис можно), через ПРОБЕЛ —
# уже не серия: «MFP 3103fdw» в разных карточках пишут и «LaserJet Pro 3103fdw», ядром служат цифры.
_ATOM_RX = re.compile(r"(?<![A-Za-z0-9])(?:([A-Za-z]{1,6})-?)?(\d{2,5})([A-Za-z]{0,6})(?![A-Za-z0-9])")
# единицы измерения сразу после числа → это не модель, а ресурс/объём/цена
_UNIT_RX = re.compile(r"^\s*(?:стр|страниц|листов|копий|мл\b|г\b|гр\b|кг\b|мм\b|см\b|шт\b|руб|₽|%|"
                      r"dpi|ppm|мин\b|год|лет|дн\b|мес)", re.I)
_MAX_PER_SRC = 120                          # предохранитель от простыни моделей в одном описании

BRANDS = ("canon", "hp", "kyocera", "epson", "brother", "samsung", "xerox", "pantum", "ricoh",
          "konica", "minolta", "oki", "lexmark", "sharp", "panasonic", "toshiba", "katusha",
          "dell", "olivetti", "develop", "utax", "sindoh", "avision", "deli")
_BRAND_RU = {"эпсон": "epson", "кэнон": "canon", "кенон": "canon", "канон": "canon",
             "куосера": "kyocera", "куасера": "kyocera", "киосера": "kyocera", "ксерокс": "xerox",
             "бразер": "brother", "самсунг": "samsung", "рико": "ricoh", "пантум": "pantum",
             "катюша": "katusha", "коника": "konica", "минолта": "minolta", "шарп": "sharp",
             "панасоник": "panasonic", "хп": "hp", "тошиба": "toshiba", "лексмарк": "lexmark",
             "оки": "oki", "делл": "dell"}
_BRAND_RX = re.compile(r"\b(" + "|".join(BRANDS) + r")\b|" +
                       r"\b(" + "|".join(_BRAND_RU) + r")\b", re.I)

# маркеры, после которых в свободном тексте перечисляют модели принтеров
_MARK_RX = re.compile(r"совместим\w*|подход\w*|подойд\w*|для\s+принтер\w*|для\s+мфу|для\s+модел\w*|"
                      r"для\s+использования|устанавливается\s+в|подходит\s+к|\bдля\b", re.I)
_SEG_LEN = 260                              # сколько символов после маркера считаем перечислением

# тип товара: порядок проверок важен (комплект → фотобарабан → головка → лента → чернила → тонер)
_KIND_RX = (
    ("kit",    re.compile(r"комплект|набор|\bcmyk\b|\b[2-9]\s*шт\b|\b[2-9]\s*картридж", re.I)),
    ("drum",   re.compile(r"фотобарабан|фотовал|драм[- ]?картридж|\bdrum\b|барабан|imaging\s*unit", re.I)),
    ("head",   re.compile(r"печатающ\w*\s*головк|printhead", re.I)),
    ("ribbon", re.compile(r"лента\s+для|риббон|\bribbon\b", re.I)),
    ("ink",    re.compile(r"чернил|\bink\b|струйн", re.I)),
    ("toner",  re.compile(r"тонер|картридж|\btoner\b|лазерн", re.I)),
)
# явный тип с Ozon (атрибут 22634) — доверяем ему больше, чем словам в названии
_OZ_TYPE = {"картридж": "toner", "тонер-картридж": "toner", "тонер": "toner",
            "комплект картриджей": "kit", "фотобарабан": "drum", "фотобарабан + картридж": "drum",
            "лента для принтера": "ribbon", "чернила": "ink", "печатающая головка": "head",
            "бункер отработанного тонера": "other"}

SRC_RANK = {"compat": 0, "cache": 1, "title": 2, "descr": 3}
_KIND_PREF = {"toner": 0, "ink": 0, "kit": 1, "drum": 2, "ribbon": 3, "head": 3, "other": 4}


def norm_brand(word):
    w = (word or "").lower()
    return _BRAND_RU.get(w, w if w in BRANDS else None)


def _brand_positions(text):
    """[(позиция, бренд)] — чтобы модели приписать БЛИЖАЙШИЙ слева бренд принтера."""
    out = []
    for m in _BRAND_RX.finditer(text or ""):
        b = norm_brand(m.group(0))
        if b:
            out.append((m.start(), b))
    return out


def parse_models(text, allow_bare=False, default_brand=None):
    """Текст → список моделей [{brand, core, norm, raw, series, digits, suffix}].

    allow_bare=False — голые числа без серии и без суффикса не берём (в описаниях это чаще ресурс
    или объём, чем модель); в структурированных полях совместимости и в вопросе покупателя берём.
    """
    t = text or ""
    brands = _brand_positions(t)
    out, seen = [], set()
    for m in _ATOM_RX.finditer(t):
        series, digits, suffix = (m.group(1) or ""), m.group(2), (m.group(3) or "")
        if _UNIT_RX.match(t[m.end():m.end() + 12]):
            continue                                   # «3800 страниц» — ресурс, не модель
        if not series and not suffix:
            if not allow_bare:
                continue
            if 1900 <= int(digits) <= 2099:            # год выпуска/гарантии
                continue
        core = (series + digits).lower()
        norm = (core + suffix).lower()
        if norm in seen:
            continue
        seen.add(norm)
        brand = default_brand
        for pos, b in brands:                          # ближайший бренд слева, но не дальше 60 символов
            if pos < m.start() and m.start() - pos <= 60:
                brand = b
        out.append({"brand": brand, "core": core, "norm": norm, "raw": m.group(0).strip(),
                    "series": series.lower(), "digits": digits, "suffix": suffix.lower()})
    return out


def compat_segments(text):
    """Куски текста после маркеров совместимости («…подходит для HP LJ 1018, 1020…»)."""
    t = text or ""
    segs, end_prev = [], -1
    for m in _MARK_RX.finditer(t):
        if m.start() < end_prev:                       # уже внутри взятого куска
            continue
        chunk = t[m.start(): m.start() + _SEG_LEN]
        cut = re.search(r"[.!?;\n]", chunk[20:])       # до конца предложения
        if cut:
            chunk = chunk[: 20 + cut.start()]
        segs.append(chunk)
        end_prev = m.start() + len(chunk)
    return segs


def detect_kind(*texts, oz_type=None):
    """Тип товара: toner|ink|drum|kit|ribbon|head|other."""
    if oz_type:
        k = _OZ_TYPE.get(oz_type.strip().lower())
        if k:
            blob = " ".join(x for x in texts if x)
            if k == "toner" and re.search(r"струйн|чернил", blob, re.I):
                return "ink"
            return k
    blob = " ".join(x for x in texts if x)
    for kind, rx in _KIND_RX:
        if rx.search(blob):
            return kind
    return "other"


# ──────────────────────────────── сборка индекса ────────────────────────────────

_COLS = ("platform", "account", "item_id", "article", "title", "url", "item_kind", "brand",
         "model_core", "model_norm", "model_raw", "src", "verdict")
_INSERT = (f"INSERT INTO compat_index ({', '.join(_COLS)}) VALUES %s "
           "ON CONFLICT (platform, item_id, model_norm, src) DO NOTHING")


def _rows_for_item(item, sources, skip_norms=()):
    """item — общие поля листинга; sources — [(src, text, use_segments)] → кортежи для вставки.

    use_segments=True для свободного текста (название, описание): модели берём ТОЛЬКО из кусков
    после маркеров совместимости, иначе в индекс поедут коды самого картриджа и ресурс в страницах.
    Структурированные поля («Совместимость картриджа» WB) читаем целиком.
    """
    rows, taken = [], set()
    for src, text, use_segments in sources:
        if not text:
            continue
        n_src = 0
        for chunk in (compat_segments(text) if use_segments else [text]):
            for md in parse_models(chunk, allow_bare=True):
                if md["norm"] in skip_norms and src in ("title", "descr"):
                    continue                            # наш собственный код картриджа, не модель принтера
                key = (md["norm"], src)
                if key in taken:
                    continue
                taken.add(key)
                n_src += 1
                rows.append((item["platform"], item["account"], str(item["item_id"]),
                             item.get("article"), (item.get("title") or "")[:300], item.get("url"),
                             item["kind"], md["brand"], md["core"], md["norm"], md["raw"][:40],
                             src, "yes"))
                if n_src >= _MAX_PER_SRC:
                    break
            if n_src >= _MAX_PER_SRC:
                break
    return rows


def _own_code_norms(*texts):
    """Нормы кодов САМОГО картриджа (vendorCode, «Название модели» Ozon) — их не считаем моделью
    принтера в названии/описании. В поля совместимости этот фильтр не лезет (там модели верные)."""
    out = set()
    for t in texts:
        for md in parse_models(t or "", allow_bare=True):
            out.add(md["norm"])
    return out


def _wb_rows(batch=400):
    """WB: характеристика «Совместимость картриджа» (главный источник) + title + описание."""
    last = 0
    while True:
        rows = db.query("""
            SELECT c.nm_id, c.account, c.vendor_code, c.payload->'characteristics' AS ch,
                   left(coalesce(c.payload->>'description',''), 4000) AS descr,
                   coalesce(w.title, c.payload->>'title') AS title, w.subject
              FROM raw_wb_card_content c
              LEFT JOIN wb_cards w ON w.nm_id = c.nm_id AND w.account = c.account
             WHERE c.nm_id > %s
             ORDER BY c.nm_id LIMIT %s""", (last, batch))
        if not rows:
            return
        last = rows[-1]["nm_id"]
        out = []
        for r in rows:
            chars = {}
            for ch in (r["ch"] or []):
                v = ch.get("value")
                chars[ch.get("name")] = " | ".join(map(str, v)) if isinstance(v, list) else str(v or "")
            compat = chars.get("Совместимость картриджа") or ""
            kind = detect_kind(r["subject"] or "", chars.get("Тип картриджа") or "", r["title"] or "")
            item = {"platform": "wb", "account": r["account"], "item_id": r["nm_id"],
                    "article": r["vendor_code"], "title": r["title"], "url": None, "kind": kind}
            skip = _own_code_norms(r["vendor_code"])
            out += _rows_for_item(item, [("compat", compat, False),
                                         ("title", r["title"], True),
                                         ("descr", r["descr"], True)], skip)
        yield out


def _oz_attr(attrs, aid):
    for a in (attrs or []):
        if str(a.get("id")) == str(aid):
            return " | ".join(str(v.get("value") or "") for v in (a.get("values") or []))
    return ""


def _rich_text(raw):
    """Rich-контент Ozon (атрибут 11254) — вытащить только текстовые куски, без разметки."""
    if not raw:
        return ""
    return " ".join(re.findall(r'"([^"]{3,200})"', raw))


def _ozon_rows(batch=300):
    """Ozon: сначала названия ВСЕХ живых листингов, затем атрибуты (собраны только по oz_acc1)."""
    last = ""
    while True:
        rows = db.query("""SELECT account, sku, offer_id, name FROM ozon_product
             WHERE NOT is_archived AND coalesce(name,'')<>'' AND sku > %s
             ORDER BY sku LIMIT %s""", (last, batch * 4))
        if not rows:
            break
        last = rows[-1]["sku"]
        out = []
        for r in rows:
            item = {"platform": "ozon", "account": r["account"], "item_id": r["sku"],
                    "article": r["offer_id"], "title": r["name"], "url": None,
                    "kind": detect_kind(r["name"])}
            out += _rows_for_item(item, [("title", r["name"], True)], _own_code_norms(r["offer_id"]))
        yield out
    last = ""
    while True:
        rows = db.query("""
            SELECT a.account, a.offer_id, a.sku, p.name,
                   (SELECT jsonb_agg(x) FROM jsonb_array_elements(a.payload->'attributes') x
                     WHERE (x->>'id') IN ('4180','4191','11254','22634','5708','9048','12141','22390','4384')
                   ) AS at
              FROM raw_ozon_attributes a
              LEFT JOIN ozon_product p ON p.offer_id = a.offer_id AND p.account = a.account
             WHERE a.offer_id > %s ORDER BY a.offer_id LIMIT %s""", (last, batch))
        if not rows:
            return
        last = rows[-1]["offer_id"]
        out = []
        for r in rows:
            at = r["at"] or []
            name = r["name"] or _oz_attr(at, 4180)
            kind = detect_kind(name, _oz_attr(at, 5708), oz_type=_oz_attr(at, 22634))
            item = {"platform": "ozon", "account": r["account"], "item_id": r["sku"],
                    "article": r["offer_id"], "title": name, "url": None, "kind": kind}
            skip = _own_code_norms(_oz_attr(at, 9048), _oz_attr(at, 12141),
                                   _oz_attr(at, 22390), _oz_attr(at, 4384), r["offer_id"])
            out += _rows_for_item(item, [("compat", _oz_attr(at, 4180), True),
                                         ("compat", _rich_text(_oz_attr(at, 11254)), True),
                                         ("descr", _oz_attr(at, 4191), True)], skip)
        yield out


def _yandex_rows(batch=400):
    """Маркет: только оферы с marketSku и витринной B2C-ссылкой (иначе покупателю нечего предложить)."""
    last = ""
    while True:
        rows = db.query("""
            SELECT o.account, o.offer_id, o.payload->'mapping'->>'marketSku' AS sku,
                   o.payload->'offer'->>'name' AS name,
                   left(coalesce(o.payload->'offer'->>'description',''), 4000) AS descr,
                   o.payload->'showcaseUrls' AS urls
              FROM raw_yandex_offer o
             WHERE o.account = 'ya_acc1' AND o.offer_id > %s
               AND o.payload->'mapping'->>'marketSku' IS NOT NULL
               AND coalesce(o.payload->'offer'->>'name','') <> ''
               AND coalesce(o.payload->'offer'->>'archived','false') <> 'true'
             ORDER BY o.offer_id LIMIT %s""", (last, batch))
        if not rows:
            return
        last = rows[-1]["offer_id"]
        out = []
        for r in rows:
            url = next((u.get("showcaseUrl") for u in (r["urls"] or [])
                        if isinstance(u, dict) and u.get("showcaseType") == "B2C"), None)
            if not url:
                continue
            item = {"platform": "yandex", "account": r["account"], "item_id": r["sku"],
                    "article": r["offer_id"], "title": r["name"], "url": url,
                    "kind": detect_kind(r["name"])}
            out += _rows_for_item(item, [("title", r["name"], True),
                                         ("descr", r["descr"], True)], _own_code_norms(r["offer_id"]))
        yield out


def _cache_rows():
    """compat_cache как обратный индекс: пара (наш листинг × модель принтера) с готовым вердиктом.

    verdict='no' попадает в индекс тоже — при подборе он ГАСИТ эту пару (уже проверяли, не подходит).
    Заголовок/тип берём из живого листинга; если листинга уже нет — строку пропускаем."""
    out = []
    for r in db.query("SELECT platform, item_id, model_norm, model_raw, verdict FROM compat_cache"):
        plat, iid = r["platform"], str(r["item_id"])
        if plat == "wb":
            got = db.query("SELECT account, vendor_code AS art, title FROM wb_cards WHERE nm_id::text=%s LIMIT 1", (iid,))
            url = None
        elif plat == "ozon":
            got = db.query("SELECT account, offer_id AS art, name AS title FROM ozon_product "
                           "WHERE sku=%s AND NOT is_archived LIMIT 1", (iid,))
            url = None
        else:
            got = db.query("""SELECT account, offer_id AS art, payload->'offer'->>'name' AS title,
                       payload->'showcaseUrls' AS urls FROM raw_yandex_offer
                     WHERE offer_id=%s OR payload->'mapping'->>'marketSku'=%s LIMIT 1""", (iid, iid))
            url = next((u.get("showcaseUrl") for u in ((got[0].get("urls") if got else None) or [])
                        if isinstance(u, dict) and u.get("showcaseType") == "B2C"), None) if got else None
        if not got:
            continue
        g = got[0]
        md = parse_models(r["model_raw"] or r["model_norm"], allow_bare=True)
        if not md:
            continue
        m = md[0]
        out.append((plat, g["account"], iid, g["art"], (g["title"] or "")[:300], url,
                    detect_kind(g["title"] or ""), m["brand"], m["core"], m["norm"],
                    (r["model_raw"] or "")[:40], "cache", r["verdict"] or "yes"))
    return out


def build(verbose=True):
    """Полная пересборка индекса. Возвращает (строк, моделей, товаров)."""
    total = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE compat_index")
            for name, gen in (("wb", _wb_rows()), ("ozon", _ozon_rows()), ("yandex", _yandex_rows())):
                n = 0
                for chunk in gen:
                    if chunk:
                        psycopg2.extras.execute_values(cur, _INSERT, chunk, page_size=1000)
                        n += len(chunk)
                total += n
                if verbose:
                    print(f"  {name}: {n} строк")
            cache = _cache_rows()
            if cache:
                psycopg2.extras.execute_values(cur, _INSERT, cache, page_size=500)
            total += len(cache)
            if verbose:
                print(f"  compat_cache: {len(cache)} строк")
            cur.execute("SELECT count(*), count(DISTINCT model_core), count(DISTINCT (platform, item_id)) "
                        "FROM compat_index")
            rows_total, models_total, items_total = cur.fetchone()
            cur.execute("""INSERT INTO compat_index_meta (id, built_at, rows_total, models_total, items_total)
                VALUES (1, now(), %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET built_at=now(), rows_total=EXCLUDED.rows_total,
                    models_total=EXCLUDED.models_total, items_total=EXCLUDED.items_total""",
                        (rows_total, models_total, items_total))
    return rows_total, models_total, items_total


# ──────────────────────────────── подбор по индексу ────────────────────────────────

_SRC_ORDER = ("ORDER BY CASE WHEN src IN ('title','compat') THEN 0 ELSE 1 END, "
              "CASE src WHEN 'compat' THEN 0 WHEN 'cache' THEN 1 WHEN 'title' THEN 2 ELSE 3 END")


def _fetch(where, params, limit=400):
    """Строки индекса под условие. Сортировка в SQL — чтобы LIMIT срезал ХВОСТ (описания),
    а не лучшие источники: у ходовых ядер («1018») строк тысячи."""
    return db.query(f"""SELECT platform, account, item_id, article, title, url, item_kind, brand,
               model_core, model_norm, src, verdict FROM compat_index WHERE {where}
               {_SRC_ORDER} LIMIT %s""", tuple(params) + (limit,))


def _brand_ok(row_brand, q_brand, strict):
    if strict:
        return bool(q_brand) and row_brand == q_brand
    return not (row_brand and q_brand and row_brand != q_brand)


def _tier_rows(q, platform, accounts):
    """Ярусы поиска: точная норма → ядро без суффикса → без серии (с брендом) → голые цифры (с брендом).

    Ярус 3 нужен там, где серию в тексте пишут через пробел («Canon 647Cdw» vs запрос «LBP-647Cdw»),
    и он ОБЯЗАТЕЛЬНО требует совпадения бренда — иначе `7180dn` от Brother поймал бы чужой `7180dn`."""
    base, params = ["platform = %s"], [platform or ""]
    if not platform:
        base, params = ["true"], []
    if accounts:
        base.append("account = ANY(%s)")
        params.append(list(accounts))
    pre = " AND ".join(base)
    # голое число без серии и без суффикса («brother 7180») само по себе ничего не значит: такое
    # совпадение принимаем ТОЛЬКО при совпавшем бренде, иначе ловится любое 7180 из чужого описания
    bare = not q["series"] and not q["suffix"]
    tiers = [(f"{pre} AND model_norm = %s", params + [q["norm"]], bare),
             (f"{pre} AND model_core = %s", params + [q["core"]], bare)]
    if q["series"] and q["suffix"]:
        tiers.append((f"{pre} AND model_norm = %s", params + [q["digits"] + q["suffix"]], True))
    if not q["series"]:
        tiers.append((f"{pre} AND model_core LIKE %s", params + ["%" + q["digits"]], True))
    return tiers


def lookup(text, platform=None, kind=None, accounts=None, color_syn=None, exclude_id=None,
           limit=8, models=None, kind_strict=False):
    """Подбор НАШИХ листингов под модель принтера из текста. → список хитов (как в catalog._search):
    {platform, id, article, title, url, kind, model, src}. Ничего не нашли → [].

    kind — нужный тип товара (drum/ink/…): такие поднимаем наверх; kind_strict=True — оставляем
    ТОЛЬКО их (вопрос про фотобарабан не должен вернуть тонер-картридж).
    color_syn — синонимы цвета: если заданы, оставляем только листинги с цветом в названии.
    models — дополнительные модели (из карточки товара), если в вопросе модели нет.
    """
    qs = parse_models(text or "", allow_bare=True)
    for extra in (models or []):
        qs += [m for m in parse_models(extra, allow_bare=True) if m["norm"] not in {x["norm"] for x in qs}]
    if not qs:
        return []
    hits, seen, banned = [], set(), set()
    for q in qs[:4]:
        rows = []
        for where, params, strict in _tier_rows(q, platform, accounts):
            rows = [r for r in _fetch(where, params) if _brand_ok(r["brand"], q["brand"], strict)]
            if kind_strict and kind:      # спросили фотобарабан — точное совпадение суффикса не
                rows = [r for r in rows if r["item_kind"] == kind]   # повод остановиться на тонерах
            if rows:
                break
        for r in rows:                                     # вердикт «не подходит» гасит пару товар×модель
            if r["verdict"] == "no":
                banned.add((r["platform"], r["item_id"]))
        for r in rows:
            key = (r["platform"], r["item_id"])
            if key in banned or key in seen or r["verdict"] == "no":
                continue
            if exclude_id and str(r["item_id"]) == str(exclude_id):
                continue
            if kind_strict and kind and r["item_kind"] != kind:
                continue
            if color_syn and not any(s.lower() in (r["title"] or "").lower() for s in color_syn):
                continue
            seen.add(key)
            # НАЗВАНИЕ важнее списка совместимости: карточка «…для Canon LBP647Cdw» — прямое попадание
            # в принтер покупателя, а та же модель в чужом списке совместимости — лишь «тоже подойдёт».
            tnorm = re.sub(r"[^a-z0-9]", "", (r["title"] or "").lower())
            in_title = 0 if q["norm"] in tnorm else (1 if q["core"] in tnorm else 2)
            hits.append({"platform": r["platform"], "id": r["item_id"], "article": r["article"],
                         "title": r["title"], "url": r["url"], "kind": r["item_kind"],
                         "model": r["model_norm"], "src": r["src"],
                         "_rank": (in_title,
                                   0 if r["src"] in ("title", "compat") else 1,
                                   SRC_RANK.get(r["src"], 9),
                                   0 if (kind and r["item_kind"] == kind) else
                                   _KIND_PREF.get(r["item_kind"], 5),
                                   len(r["title"] or ""))})
    hits = [h for h in hits if (h["platform"], h["id"]) not in banned]
    hits.sort(key=lambda h: h["_rank"])
    for h in hits:
        h.pop("_rank", None)
    return hits[:limit]


_READY = None


def is_ready():
    """Индекс собран? (пустая таблица = откат на старый ILIKE-путь, чтобы подбор не пропал).
    Ответ кэшируется в процессе: бот живёт долго, а таблица меняется только пересборкой."""
    global _READY
    if _READY is None:
        try:
            _READY = bool(db.query("SELECT 1 FROM compat_index LIMIT 1"))
        except Exception:
            _READY = False
    return _READY


def stats():
    q = db.query("""SELECT count(*) rows_total, count(DISTINCT model_core) models,
              count(DISTINCT (platform, item_id)) items FROM compat_index""")[0]
    per_src = db.query("SELECT src, count(*) c FROM compat_index GROUP BY 1 ORDER BY 2 DESC")
    per_plat = db.query("SELECT platform, count(DISTINCT item_id) c FROM compat_index GROUP BY 1")
    return q, per_src, per_plat


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--build" in args:
        print("Сборка индекса совместимости…")
        r, m, i = build()
        print(f"Готово: строк {r}, моделей {m}, товаров {i}")
    elif "--stats" in args:
        q, per_src, per_plat = stats()
        print(f"строк {q['rows_total']}, моделей {q['models']}, товаров {q['items']}")
        print("  по источникам:", ", ".join(f"{r['src']}={r['c']}" for r in per_src))
        print("  по площадкам:", ", ".join(f"{r['platform']}={r['c']}" for r in per_plat))
    elif "--check" in args:
        i = args.index("--check")
        qtext = args[i + 1]
        plat = args[i + 2] if len(args) > i + 2 else None
        res = lookup(qtext, platform=plat)
        print(f"{qtext} [{plat or 'любая'}] → {len(res)} хитов")
        for h in res[:5]:
            print(f"  {h['platform']} {h['id']} ({h['kind']}, {h['src']}) {(h['title'] or '')[:70]}")
    else:
        print(__doc__.strip().splitlines()[-3])
