"""reports/card_facts.py — ЧИСТОЕ извлечение фактов карточки для движка вопросов (источник №2).

Заменяет регекс-по-прозе из feedback_grounding: читает СТРУКТУРУ карточки.
- Ozon (raw_ozon_attributes): карта attribute-id. Главное — виджет 11254 (raTextBlock):
  блок «❗️ Для принтеров ❗️» = чистый список ✅ моделей; спецблок = строки «Ресурс: N»,
  «Чип: …». Плюс поля: 5708 тип, 5709 ресурс, 5713 совместимый/оригинал, 9602 цвет,
  4180 имя, 12141/9048 артикул, 23171 хэштеги моделей (+ #с_чипом), 4191 аннотация.
- WB (raw_wb_card_content): characteristics по имени + модели из title/description.

Чип — 3 состояния (клиент требует прямой ответ, не уклончивый):
  installed         — «с чипом», «оснащён чипом», #с_чипом, «в комплекте чип»
  not_required      — «не требуется чип», «полностью готов к использованию» (докупать не надо)
  none              — «без чипа», «чип отсутствует»
  None              — в карточке нет сигнала (→ движок эскалирует, не выдумывает)

Возвращает dict фактов или None (карточки нет). Значение — только доказанное структурой.
"""
import re
import json
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

# --- карта attribute-id Ozon ---
OZ = dict(name=4180, article=12141, vcode=9048, ptype=5708, kind=5713,
          resource=5709, color=9602, colormode=10472, annot=4191, rich=11254,
          hashtags=23171, pack=22634)


def _oz_vals(attrs):
    """attribute_id -> список строковых значений."""
    out = {}
    for a in attrs or []:
        out.setdefault(a.get("id"), []).extend(
            str(v.get("value", "")) for v in a.get("values", []) if v.get("value") is not None)
    return out


def _first(vals, aid):
    v = vals.get(aid)
    return v[0].strip() if v and v[0] else None


def _walk_widget(rich_json):
    """Из виджета raTextBlock достаёт (models[], spec_lines[])."""
    models, specs = [], []
    try:
        data = json.loads(rich_json)
    except (ValueError, TypeError):
        return models, specs
    for w in data.get("content", []):
        title = " ".join((w.get("title") or {}).get("content", []) or [])
        lines = (w.get("text") or {}).get("content", []) or []
        if "для принтер" in title.lower() or "для мфу" in title.lower():
            for ln in lines:
                ln = ln.strip()
                if ln.startswith("✅"):
                    models.append(ln.lstrip("✅").strip())
        for ln in lines:
            if ":" in ln and any(k in ln.lower() for k in ("чип", "ресурс", "ёмкост", "емкост")):
                specs.append(ln.strip())
    return models, specs


def _classify_chip(text):
    t = (text or "").lower()
    if re.search(r"без\s+чип|чип\s+отсутств|нет\s+чип(?!\w)", t):
        return "none"
    if re.search(r"с\s*чипом|оснащ\w*\s+чип|#с_чипом|в\s+комплект\w*\s*.{0,15}чип|чип\s*[:\-]?\s*с\s*чипом", t):
        return "installed"
    if re.search(r"не\s+требуется\s+чип|чип\s+не\s+требуется|полностью\s+готов", t):
        return "not_required"
    return None


def _ptype(s):
    s = (s or "").lower()
    if "струйн" in s:
        return "струйный/чернила"
    if "лазерн" in s:
        return "тонер/лазерный"
    return None


def facts_ozon(payload):
    attrs = payload.get("attributes") or []
    if not attrs:
        return None
    vals = _oz_vals(attrs)
    rich = _first(vals, OZ["rich"]) or ""
    annot = _first(vals, OZ["annot"]) or ""
    tags = " ".join(vals.get(OZ["hashtags"], []))
    models, specs = _walk_widget(rich)
    chip = _classify_chip(" || ".join(specs) + " || " + annot + " || " + tags)
    # ресурс: спец-строка виджета → поле 5709
    resource = None
    for s in specs:
        m = re.search(r"ресурс\D*(\d[\d\s]{1,7})", s.lower())
        if m:
            resource = m.group(1).replace(" ", "")
            break
    if not resource:
        resource = _first(vals, OZ["resource"])
    refill = "да" if re.search(r"заправочн|заправляем|для заправк|дозаправ", (annot + payload.get("name", "")).lower()) else None
    return {
        "platform": "ozon",
        "name": _first(vals, OZ["name"]) or payload.get("name"),
        "article": _first(vals, OZ["article"]) or _first(vals, OZ["vcode"]),
        "code": _cart_code(payload.get("name"), _first(vals, OZ["name"]), annot),
        "models": models,
        "chip": chip,
        "resource": resource,
        "ptype": _ptype(_first(vals, OZ["ptype"])),
        "kind": _first(vals, OZ["kind"]),
        "color": _first(vals, OZ["color"]),
        "refillable": refill,
        "annot": annot[:600],
    }


# --- WB ---
def _wb_char(chars, *names):
    for c in chars or []:
        if (c.get("name") or "").strip().lower() in [n.lower() for n in names]:
            v = c.get("value")
            if isinstance(v, list):
                v = v[0] if v else None
            return str(v).strip() if v not in (None, "") else None
    return None


def _models_from_title(title):
    """Модели из «… для <brand model, model2>»."""
    t = title or ""
    m = re.search(r"\bдля\s+(.+)$", t, re.I)
    if not m:
        return []
    tail = re.split(r"\bчерн|\bцветн|\bжелт|\bголуб|\bпурпур|\bматов", m.group(1))[0]
    return [p.strip() for p in re.split(r"[,/]| и ", tail) if len(p.strip()) > 2][:12]


# триггеры перечня совместимых моделей внутри описания WB
_DESC_TRIG = re.compile(
    r"(?:совместим\w*(?:\s+с)?(?:\s+так\w+)?\s*модел\w*(?:\s+как)?|"
    r"совместим\w+\s+с|подход\w+\s+(?:для|к)|устанавлив\w+\s+в|"
    r"для\s+принтеров?|для\s+мфу|перечень\s+моделей|список\s+моделей|"
    r"совместимость)\s*[:\-—]?\s*", re.I)
# модель-код: содержит цифру, буквенно-цифровой (MF3010, TASKalfa 181, CP1500)
_MODEL_TOK = re.compile(r"[A-Za-zА-Яа-я].*\d|\d.*[A-Za-zА-Яа-я]")


# обрезка прозаического хвоста внутри токена модели
_TOK_CUT = re.compile(r"\s+(?:имеет|цвет|ресурс|стр\b|заправ|оригинал|совместим|подход|"
                      r"чёрн|черн|голуб|пурпур|жёлт|желт|и\s+другие).*$", re.I)


def _models_from_desc(desc):
    """Полный список совместимых моделей из прозы описания (не только title)."""
    d = desc or ""
    out = []
    for m in _DESC_TRIG.finditer(d):
        tail = d[m.end():m.end() + 300]
        tail = re.split(r"[.!\n;•]|(?<!\d)\s—\s", tail)[0]
        for tok in re.split(r"[,/]|\s+и\s+", tail):
            tok = re.sub(r"^\s*(?:как|таких|такие)\s+", "", tok, flags=re.I)  # остаток «…, как X»
            tok = _TOK_CUT.sub("", tok)                                       # «3000 имеет цвет…»→«3000»
            tok = re.sub(r"\s{2,}", " ", tok).strip(" .,:;()«»\"'")
            if 2 < len(tok) <= 40 and _MODEL_TOK.search(tok) and tok.lower() not in ("для", "и", "как"):
                out.append(tok)
    return out


def _norm_art(a):
    """Нормализованный ключ артикула для сшивки WB↔Ozon-двойника."""
    return re.sub(r"[^0-9a-zа-я]", "", (a or "").lower()) or None


# код САМОГО картриджа (не имя принтера): Epson S050xxx/C13S0…, HP CF/CE/CB/Q…, Canon CRG/C-EXV/№,
# Kyocera TK, Brother TN/DR, Samsung CLT/MLT, Ricoh SP/типы. Берём первый уверенный шаблон.
_CODE_RX = re.compile(
    r"\b(C13S0\d{5}|S0?50\d{3}|CF\d{3}[A-Z]|CE\d{3}[A-Z]|CB\d{3}[A-Z]|Q\d{4}[A-Z]|"
    r"CLT-?[A-Z]\d{3}[A-Z]?|MLT-?D\d{3}[A-Z]?|TK-?\d{3,4}[A-Z]*|TN-?\d{3,4}[A-Z]*|"
    r"DR-?\d{3,4}[A-Z]*|CRG-?\d{3}[A-Z]*|C-EXV\s?\d{1,2}|GPR-?\d{1,2}|"
    r"CLI-?\d{1,3}[A-Z]*|PGI-?\d{1,3}[A-Z]*|K[PC]-?\d{2,3}I[PN]|RP-?\d{2,4})\b", re.I)


def _cart_code(*texts):
    for t in texts:
        m = _CODE_RX.search(t or "")
        if m:
            return re.sub(r"\s+", "", m.group(1)).upper()
    return None


def _merge_models(*lists):
    seen, out = set(), []
    for lst in lists:
        for x in lst or []:
            k = re.sub(r"\s+", " ", x.lower()).strip()
            if k and k not in seen:
                seen.add(k)
                out.append(x.strip())
    return out[:20]


def facts_wb(payload):
    chars = payload.get("characteristics") or []
    title = payload.get("title") or ""
    desc = payload.get("description") or ""
    chip = _classify_chip(desc + " || " + title)
    refill = "да" if re.search(r"заправочн|заправляем|для заправк|дозаправ", (desc + title).lower()) else None
    return {
        "platform": "wb",
        "name": title,
        "article": _wb_char(chars, "Модель") or payload.get("vendorCode"),
        "vcode": payload.get("vendorCode"),
        "code": _cart_code(title, desc, _wb_char(chars, "Комплектация")),
        "models": _merge_models(_models_from_title(title), _models_from_desc(desc)),
        "chip": chip,
        "resource": _wb_char(chars, "Максимальный ресурс", "Ресурс"),
        "ptype": _ptype(_wb_char(chars, "Технология печати принтера", "Тип печати")),
        "kind": "совместимый" if "совместим" in desc.lower() else None,
        "color": _wb_char(chars, "Цвет картриджа/чернил", "Цвет"),
        "refillable": refill,
        "annot": desc[:600],
    }


class CardFacts:
    """Ленивая загрузка индексов карточек. for_ozon(sku) / for_wb(nm)."""

    def __init__(self):
        self._oz = None      # offer_id -> payload
        self._oz_by_sku = None
        self._oz_by_art = None   # норм-артикул -> facts (для WB-двойника)
        self._wb = None      # nm_id -> payload

    def _ensure_oz(self):
        if self._oz is None:
            self._oz, self._oz_by_sku = {}, {}
            for r in db.query("SELECT offer_id, sku, payload FROM raw_ozon_attributes WHERE account='oz_acc1'"):
                self._oz[str(r["offer_id"])] = r["payload"]
                if r["sku"]:
                    self._oz_by_sku[str(r["sku"])] = r["payload"]

    def _ensure_oz_art(self):
        """Индекс Ozon-фактов по норм-артикулу — источник чипа/ресурса для WB-двойника."""
        if self._oz_by_art is None:
            self._ensure_oz()
            self._oz_by_art = {}
            for p in self._oz.values():
                f = facts_ozon(p)
                if not f:
                    continue
                a = _norm_art(f.get("article"))
                if a and a not in self._oz_by_art:
                    self._oz_by_art[a] = f

    def _ensure_wb(self):
        if self._wb is None:
            self._wb = {str(r["nm_id"]): r["payload"]
                        for r in db.query("SELECT nm_id, payload FROM raw_wb_card_content WHERE account='wb_acc1'")}

    def for_ozon(self, sku):
        self._ensure_oz()
        p = self._oz_by_sku.get(str(sku))
        if p is None:                      # sku→offer_id через ozon_product
            r = db.query("SELECT offer_id FROM ozon_product WHERE sku=%s AND account='oz_acc1'", (str(sku),))
            if r:
                p = self._oz.get(str(r[0]["offer_id"]))
        return facts_ozon(p) if p else None

    def for_wb(self, nm_id):
        self._ensure_wb()
        p = self._wb.get(str(nm_id))
        if not p:
            return None
        f = facts_wb(p)
        # добор из Ozon-двойника того же артикула, когда WB-карточка молчит
        if f.get("chip") is None or not f.get("resource") or not f.get("models"):
            self._ensure_oz_art()
            twin = None
            for key in (f.get("article"), f.get("vcode")):
                twin = self._oz_by_art.get(_norm_art(key))
                if twin:
                    break
            if twin:
                if f.get("chip") is None and twin.get("chip"):
                    f["chip"] = twin["chip"]
                    f["chip_src"] = "ozon-двойник"
                if not f.get("resource") and twin.get("resource"):
                    f["resource"] = twin["resource"]
                if not f.get("models") and twin.get("models"):
                    f["models"] = twin["models"]
                    f["models_src"] = "ozon-двойник"
        return f


if __name__ == "__main__":
    cf = CardFacts()
    for sku in ["1611110080", "1692352035", "866443528", "873314556", "1153395894"]:
        print(sku, "->", cf.for_ozon(sku))
