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
_BRANDS = ("canon", "hp", "kyocera", "epson", "brother", "samsung", "xerox",
           "pantum", "ricoh", "konica", "minolta", "oki", "lexmark", "sharp", "panasonic")


_STOP = {"для", "мфу", "модель", "моделей", "серии", "при", "как", "под", "или", "это", "нет"}


def _model_tokens(text):
    """Модель-подобные токены вопроса: цифросодержащие (MF3010, TK-1170, 737) + серия-префикс перед
    числом (PSC 750 → psc, делает поиск строже, режет ложные матчи по одной цифре) + бренды."""
    t = text or ""
    toks = []
    for m in re.finditer(r"[A-Za-zА-Яа-я0-9][\w\-]*", t):
        w = m.group(0)
        if re.search(r"\d", w) and len(w) >= 2 and not re.fullmatch(r"\d{1,2}", w):
            toks.append(w)
    low = t.lower()
    brands = [b for b in _BRANDS if b in low]
    prefixes = [m.group(1) for m in re.finditer(r"\b([A-Za-zА-Яа-я]{2,10})\s?-?\d{2,5}\b", t)
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


def catalog_block(text, product_name="", card_models=None):
    """Блок КАТАЛОГ для промпта или '' — если вопрос не про наличие/варианты либо ничего не нашлось.

    Модель принтера берём из ВОПРОСА, иначе из моделей карточки / названия товара (частый кейс:
    «а цветной есть?» — модель мы уже знаем из карточки, спрашивать её у покупателя не нужно)."""
    q = (text or "")
    color = _detect_color(q)
    if not (_AVAIL_RX.search(q) or color or _ALT_RX.search(q)):
        return ""
    brand = next((b for b in _BRANDS if b in (q + " " + (product_name or "")).lower()), None)
    # номер(а) модели: из вопроса → из моделей карточки → из названия товара
    nums = _nums(_model_tokens(q))
    if not nums:
        pool = " ".join([product_name or ""] + list(card_models or []))
        nums = _nums(_model_tokens(pool))
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
    if not hits:
        return ""
    want = f" ({color})" if color else ""
    lines = [f"КАТАЛОГ — наши листинги под запрос{want} (можно назвать артикул/подтвердить наличие; "
             "если подходящего варианта здесь нет — наличие НЕ утверждать, предложи уточнить):"]
    for h in hits[:8]:
        art = h["article"] or "—"
        lines.append(f"- [{h['platform']} id {h['id']}] {(h['title'] or '')[:90]} — арт. {art}")
    return "\n".join(lines)


if __name__ == "__main__":
    for q in ["Есть ли у вас этот картридж штучно чёрный?",
              "Нужен только голубой для Kyocera TK-5240, есть артикул?",
              "подходит ли для Canon MF3010?"]:
        print("Q:", q)
        print(catalog_block(q) or "  (блок пуст)")
        print()
