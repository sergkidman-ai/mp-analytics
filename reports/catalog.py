"""reports/catalog.py — поиск по НАШИМ живым листингам (WB + Ozon) для вопросов о наличии.

Источник №3 движка вопросов. Отвечает на «есть ли у вас чёрный отдельно / штучно / артикул
на модель X»: ищет по title карточек wb_cards и name листингов ozon_product (то, что реально
продаётся) по модель-токенам из вопроса + цвету. Возвращает кандидатов (площадка, id, название,
артикул) — их движок кладёт в промпт, чтобы модель могла назвать артикул/подтвердить наличие,
не выдумывая. Ничего не находит → пустой блок (движок не утверждает наличие).
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

# цвет → синонимы для ILIKE по названию листинга
COLORS = {
    "чёрный":   ["чёрн", "черн", "black"],
    "голубой":  ["голуб", "циан", "cyan"],
    "пурпурный": ["пурпур", "малин", "magenta"],
    "жёлтый":   ["жёлт", "желт", "yellow"],
    "цветной":  ["цветн", "трёхцвет", "трехцвет", "многоцвет", "color", "cmy"],
}
# порядок важен: «не цветной» → чёрный ловим ПЕРЕД «цветной»
_COLOR_TRIG = {
    "чёрный": r"чёрн|черн|black|\bbk\b|не\s*цветн|чёрно-?бел|черно-?бел|\bч[/\\]?б\b|монохром",
    "голубой": r"голуб|циан|cyan",
    "пурпурный": r"пурпур|малин|magenta",
    "жёлтый": r"жёлт|желт|yellow",
    "цветной": r"цветн|трёхцвет|трехцвет|многоцвет",
}
# запрос про наличие/штучно/артикул
_AVAIL_RX = re.compile(
    r"есть\s+ли|в\s+наличи|штучн|поштучн|по\s+отдельн|отдельн|раздельн|"
    r"продаёте|продаете|можно\s+(?:ли\s+)?купить|нужен\s+только|нужен\s+артикул|"
    r"артикул|каталог|другие\s+цвета|остальные\s+цвета", re.I)
# компат/альтернатива: покупатель ищет ПОДХОДЯЩИЙ картридж под свою модель (в т.ч. «если нет — какой?»).
# Триггерит поиск в НАШЕМ ассортименте по модели принтера из вопроса → предложить наш артикул.
_ALT_RX = re.compile(
    r"подойд|подход|совмест|как(?:ой|ая|ое|ие)\b|что\s+подойд|чем\s+замен|"
    r"если\s+нет|имеют?ся?\s+ли|\bарт\b|подскажите|нужен\s+картридж|нужен\s+другой|"
    r"что\s+(?:нужно|взять|купить)|какой\s+нужен", re.I)
# сопутствующий расходник: покупатель спрашивает про фотобарабан/девелопер/печку — если он есть у нас
# под ту же модель, предложим его площадочный артикул. Слово → ILIKE-синонимы для поиска по нашим листингам.
_ACCESSORY = {
    "фотобарабан": ["фотобарабан", "фотобарабана", "барабан", "drum", "имидж"],
    "девелопер":   ["девелопер", "developer", "проявк", "фотовал"],
    "печка":       ["печк", "термоуз", "термоблок", "fuser", "фьюзер"],
}
_ACC_RX = re.compile(r"фотобарабан|барабан|\bdrum\b|девелопер|проявк|фотовал|печк|термоуз|термоблок|фьюзер", re.I)
# вопрос о ПОЛНОТЕ цветов: «достаточно ли одного?», «нужны ли другие цвета?», «нужен ли чёрный?».
# Товар цветной (CMY) → печать чёрного текста требует отдельного чёрного; модель принтера знаем из
# названия товара → подбираем чёрный из каталога, НЕ переспрашивая модель.
_COMPLETE_RX = re.compile(r"достаточно|хватит\s+ли|нужны?\s+ли\s+(?:ещё|еще|други|отдельн|чёрн|черн)|"
                          r"нужен\s+ли\s+(?:ещё|еще|отдельн|чёрн|черн|чб)|только\s+этот|одного\s+картридж", re.I)
_BRANDS = ("canon", "hp", "kyocera", "epson", "brother", "samsung", "xerox",
           "pantum", "ricoh", "konica", "minolta", "oki", "lexmark", "sharp", "panasonic")


_STOP = {"для", "мфу", "модель", "моделей", "серии", "при", "как", "под", "или", "это", "нет"}

# кириллица→латиница для модель-кодов (покупатель пишет «сх3900», в наших title латиница «CX3900»)
_CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "з": "z", "и": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "c", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "c", "ч": "ch", "ш": "sh", "ы": "y", "э": "e", "ю": "yu", "я": "ya"})
# бренды по-русски → латиница
_BRAND_RU = {"эпсон": "epson", "кэнон": "canon", "кенон": "canon", "куосера": "kyocera", "куасера": "kyocera",
             "ксерокс": "xerox", "бразер": "brother", "самсунг": "samsung", "рико": "ricoh", "пантум": "pantum",
             "катюша": "katusha", "коника": "konica", "минолта": "minolta", "шарп": "sharp", "панасоник": "panasonic"}


def _translit_code(w):
    """Токен модели с кириллицей → латиница (сх3900→cx3900); латинский оставляем как есть."""
    if re.search(r"[а-яё]", w.lower()):
        return w.lower().translate(_CYR2LAT)
    return w


def _model_tokens(text):
    """Модель-подобные токены вопроса: цифросодержащие (MF3010, TK-1170, 737) + серия-префикс перед
    числом (PSC 750 → psc, делает поиск строже, режет ложные матчи по одной цифре) + бренды.
    Кириллические модель-коды транслитерируются в латиницу (в наших title модели латиницей)."""
    t = text or ""
    low = t.lower()
    for ru, en in _BRAND_RU.items():          # эпсон→epson и т.п., чтобы бренд нашёлся
        if ru in low:
            t = t + " " + en
    toks = []
    for m in re.finditer(r"[A-Za-zА-Яа-я0-9][\w\-]*", t):
        w = m.group(0)
        if re.search(r"\d", w) and len(w) >= 2 and not re.fullmatch(r"\d{1,2}", w):
            toks.append(_translit_code(w))    # кир→лат для поиска по латинскому title
    low = t.lower()
    brands = [b for b in _BRANDS if b in low]
    prefixes = [_translit_code(m.group(1)) for m in re.finditer(r"\b([A-Za-zА-Яа-я]{2,10})\s?-?\d{2,5}\b", t)
                if m.group(1).lower() not in _STOP and m.group(1).lower() not in _BRANDS]
    seen, out = set(), []
    for x in prefixes + brands + toks:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:6]


def _detect_color(text):
    low = (text or "").lower()
    for col, rx in _COLOR_TRIG.items():
        if re.search(rx, low):
            return col
    return None


def _search(model_tokens, color, limit=8):
    """AND по модель-токенам + (опц.) цвет. Возвращает кандидатов из wb_cards и ozon_product."""
    if not model_tokens and not color:
        return []
    out = []
    # WB
    conds, params = [], []
    for t in model_tokens:
        conds.append("title ILIKE %s")
        params.append("%" + t + "%")
    if color:
        syn = COLORS[color]
        conds.append("(" + " OR ".join(["title ILIKE %s"] * len(syn)) + ")")
        params += ["%" + s.strip() + "%" for s in syn]
    where = " AND ".join(conds) if conds else "true"
    for r in db.query(f"""SELECT nm_id, vendor_code, title FROM wb_cards
        WHERE {where} AND coalesce(title,'')<>'' ORDER BY nm_id DESC LIMIT %s""", tuple(params) + (limit,)):
        out.append({"platform": "wb", "id": r["nm_id"], "article": r["vendor_code"], "title": r["title"]})
    # Ozon
    conds, params = [], []
    for t in model_tokens:
        conds.append("name ILIKE %s")
        params.append("%" + t + "%")
    if color:
        syn = COLORS[color]
        conds.append("(" + " OR ".join(["name ILIKE %s"] * len(syn)) + ")")
        params += ["%" + s.strip() + "%" for s in syn]
    where = " AND ".join(conds) if conds else "true"
    for r in db.query(f"""SELECT sku, offer_id, name FROM ozon_product
        WHERE account='oz_acc1' AND {where} AND coalesce(name,'')<>'' AND NOT is_archived
        ORDER BY sku DESC LIMIT %s""", tuple(params) + (limit,)):
        out.append({"platform": "ozon", "id": r["sku"], "article": r["offer_id"], "title": r["name"]})
    return out[: limit + 4]


def _nums(toks):
    return [t for t in toks if re.search(r"\d", t)]


def _detect_accessory(text):
    """Какой сопутствующий расходник спрашивают (фотобарабан/девелопер/печка) → ключ или None."""
    low = (text or "").lower()
    for key, syns in _ACCESSORY.items():
        if any(s in low for s in syns):
            return key
    return None


def _search_accessory(model_tokens, acc_key, limit=6):
    """Наши листинги сопутствующего типа (фотобарабан/девелопер/печка) под модель принтера."""
    syns = _ACCESSORY[acc_key]
    out = []
    for tbl, idc, artc, namec, extra in (
            ("wb_cards", "nm_id", "vendor_code", "title", ""),
            ("ozon_product", "sku", "offer_id", "name", "AND account='oz_acc1' AND NOT is_archived")):
        conds, params = [], []
        for t in model_tokens:
            conds.append(f"{namec} ILIKE %s"); params.append("%" + t + "%")
        conds.append("(" + " OR ".join([f"{namec} ILIKE %s"] * len(syns)) + ")")
        params += ["%" + s + "%" for s in syns]
        where = " AND ".join(conds)
        for r in db.query(f"""SELECT {idc} id, {artc} art, {namec} nm FROM {tbl}
            WHERE {where} AND coalesce({namec},'')<>'' {extra} ORDER BY {idc} DESC LIMIT %s""",
                          tuple(params) + (limit,)):
            plat = "wb" if tbl == "wb_cards" else "ozon"
            out.append({"platform": plat, "id": r["id"], "article": r["art"], "title": r["nm"]})
    return out


def _plat_ref(h):
    """Идентификатор ДЛЯ ПОКУПАТЕЛЯ (по нему реально найдёт), НЕ наш внутренний offer_id/vendorCode:
    WB → артикул ВБ (nm_id) + ссылка; Ozon → SKU + ссылка."""
    if h["platform"] == "wb":
        return (f"артикул ВБ {h['id']}",
                f"https://www.wildberries.ru/catalog/{h['id']}/detail.aspx")
    return (f"Ozon SKU {h['id']}", f"https://www.ozon.ru/product/{h['id']}")


def catalog_block(text, product_name="", card_models=None, platform=None, card_color=None):
    """Блок КАТАЛОГ для промпта или '' — если вопрос не про наличие/варианты либо ничего не нашлось.

    Модель принтера берём из ВОПРОСА, иначе из моделей карточки / названия товара (частый кейс:
    «а цветной есть?» — модель мы уже знаем из карточки, спрашивать её у покупателя не нужно).
    platform — площадка покупателя (wb/ozon): её листинги показываем ПЕРВЫМИ (там он и купит)."""
    q = (text or "")
    color = _detect_color(q)
    acc = _detect_accessory(q)
    # вопрос о полноте цветов у ЦВЕТНОГО товара → ищем чёрный под модель из названия товара
    prod = (product_name or "").lower() + " " + (card_color or "").lower()
    is_color_prod = bool(re.search(r"цветн|трёхцвет|трехцвет|cmy|многоцвет|color", prod))
    if _COMPLETE_RX.search(q) and is_color_prod and not color:
        color = "чёрный"
    if not (_AVAIL_RX.search(q) or color or _ALT_RX.search(q) or acc):
        return ""
    brand = next((b for b in _BRANDS if b in (q + " " + (product_name or "")).lower()), None)
    # номер(а) модели: из вопроса → из моделей карточки → из названия товара
    nums = _nums(_model_tokens(q))
    pool_nums = _nums(_model_tokens(" ".join([product_name or ""] + list(card_models or []))))
    if not nums:
        nums = pool_nums
    # сопутствующий расходник (фотобарабан/девелопер) — модель принтера берём из карточки товара
    acc_hits = []
    if acc:
        for num in ((nums or pool_nums)[:3] or [None]):
            toks = [x for x in (brand, num) if x]
            if toks:
                acc_hits += _search_accessory(toks, acc)
        seen_a = set()
        acc_hits = [h for h in acc_hits if (h["platform"], h["id"]) not in seen_a and not seen_a.add((h["platform"], h["id"]))]
        if platform:
            acc_hits.sort(key=lambda h: 0 if h["platform"] == platform else 1)
    hits, seen = [], set()
    # поиск ПО КАЖДОЙ модели отдельно (brand+номер+цвет) — union, не жёсткий AND всех номеров
    for num in (nums[:3] or [None]):
        toks = [x for x in (brand, num) if x]
        for h in _search(toks, color):
            key = (h["platform"], h["id"])
            if key not in seen:
                seen.add(key)
                hits.append(h)
    if not hits and color:               # цвета не нашли — попробуем шире (вдруг листинг без слова-цвета)
        for num in (nums[:3] or [None]):
            toks = [x for x in (brand, num) if x]
            for h in _search(toks, None):
                key = (h["platform"], h["id"])
                if key not in seen:
                    seen.add(key)
                    hits.append(h)
    if not hits and not acc_hits:
        return ""
    want = f" ({color})" if color else ""
    lines = []
    if hits:
        # площадку покупателя — вперёд (там он и оформит заказ)
        if platform:
            hits.sort(key=lambda h: 0 if h["platform"] == platform else 1)
        lines.append(f"КАТАЛОГ — наши листинги под запрос{want}. Покупателю называй АРТИКУЛ ПЛОЩАДКИ (по "
                     "нему он найдёт товар) и/или ссылку — НЕ наш внутренний код. Если подходящего варианта "
                     "здесь нет — наличие НЕ утверждать, предложи уточнить:")
        for h in hits[:8]:
            ref, url = _plat_ref(h)
            lines.append(f"- {(h['title'] or '')[:80]} — {ref}, ссылка {url}")
    if acc_hits:
        lines.append(f"КАТАЛОГ — сопутствующий товар ({acc}) у нас есть под эту модель, можно предложить "
                     "(артикул площадки + ссылка):")
        for h in acc_hits[:5]:
            ref, url = _plat_ref(h)
            lines.append(f"- {(h['title'] or '')[:80]} — {ref}, ссылка {url}")
    return "\n".join(lines)


if __name__ == "__main__":
    for q in ["Есть ли у вас этот картридж штучно чёрный?",
              "Нужен только голубой для Kyocera TK-5240, есть артикул?",
              "подходит ли для Canon MF3010?"]:
        print("Q:", q)
        print(catalog_block(q) or "  (блок пуст)")
        print()
