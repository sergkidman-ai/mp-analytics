"""reports/feedback_grounding.py — grounding-слой: факты о товаре из карточек МойСклада.

Зачем: чтобы ответчик на отзывы/вопросы отвечал ПО СУЩЕСТВУ (есть ли чип, заправляемый ли,
какие модели совместимы, ресурс), а не уклончиво. См. память feedback_answer_engine.

Модель данных МойСклада (важно): продаваемая карточка = числовой `code` (0157, 5500) — у неё
описание тонкое/пустое. Богатые описания (с чипом / совместимый / модели / ресурс) лежат на
ВАРИАНТАХ ПОСТАВЩИКОВ: код = числовой префикс + суффикс (0157ct, 0157bs, 1328ep...). Плюс наборы
(842451-842454, C-EXV65) — компоненты под другими кодами, их достаём по артикулу картриджа из
названия. Межбрендовое протекание душим гардом по бренду.

Приоритетный источник — РОДНАЯ карточка площадки (то, что видит покупатель), собранная
коллекторами wb_card_content / ozon_attributes в raw_wb_card_content / raw_ozon_attributes.
Она идёт seed'ом (гарантированный источник), поверх — родственники из МС:
  Ozon: sku → ozon_product.offer_id → карточка Ozon (attributes) + карточки МС по префиксу/артикулу.
  WB:   nm → карточка WB (описание+характеристики) + баркод → ms_id → карточка МС.
Если родной карточки нет — фолбэк на прежнюю логику (МС по баркоду/префиксу/артикулу).

Запуск для проверки:  ./venv/bin/python reports/feedback_grounding.py [sku ...]
"""
import re
import sys
import json
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

BRANDS = ["hp", "canon", "epson", "samsung", "brother", "kyocera", "xerox", "ricoh",
          "pantum", "panasonic", "oki", "lexmark", "toshiba", "sharp", "ricoh", "konica"]


def _brand(text):
    t = (text or "").lower()
    for b in BRANDS:
        if b in t:
            return b
    return None


def _code_prefix(code):
    m = re.match(r"^(\d+)", str(code or ""))
    return m.group(1) if m else None


def _cart_tokens(name):
    """Артикулы картриджа из НАЗВАНИЯ до слова «для» (не модели принтера — те брендо-неоднозначны).
    Пример: 'Картридж T0922 для Epson…' → {t0922}; 'C-EXV65 для Canon…' → {c-exv65, exv65}."""
    head = re.split(r"\bдля\b", str(name or ""), 1, flags=re.I)[0]
    toks = set()
    # буквенно-цифровые коды: T0922, C-EXV65, CLT-404S, MLT-D111S, KX-FAT410A
    for m in re.findall(r"\b([A-Za-z]{1,4}-?[A-Za-z]{0,4}\d{2,5}[A-Za-z]?)\b", head):
        ml = m.lower()
        if re.search(r"\d", ml):
            toks.add(ml)
            toks.add(re.sub(r"[a-z]$", "", ml))              # без хвостовой буквы (CLT-404S→CLT-404)
            if "-" in ml:
                toks.add(ml.split("-", 1)[1])                # C-EXV65 → exv65
    # чисто числовые артикулы наборов: 842451
    for m in re.findall(r"\b(\d{6})\b", head):
        toks.add(m)
    return {t for t in toks if len(t) >= 4}


def _wb_card_blob(payload):
    """Плоский текст из карточки WB: заголовок + описание + характеристики (имя: значения)."""
    p = json.loads(payload) if isinstance(payload, str) else payload
    parts = [p.get("title") or "", p.get("description") or ""]
    for ch in (p.get("characteristics") or []):
        v = ch.get("value")
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        if v:
            parts.append(f"{ch.get('name', '')}: {v}")
    return re.sub(r"\s+", " ", " | ".join(x for x in parts if x)).strip()


def _oz_attr_blob(payload):
    """Плоский текст из карточки Ozon: имя + значения всех атрибутов (модели/тип/ресурс/аннотация)."""
    p = json.loads(payload) if isinstance(payload, str) else payload
    parts = [p.get("name") or ""]
    for a in (p.get("attributes") or []):
        vals = [str(v.get("value")) for v in (a.get("values") or []) if v.get("value")]
        if vals:
            parts.append(" ".join(vals))
    return re.sub(r"\s+", " ", " | ".join(x for x in parts if x)).strip()


class Grounding:
    def __init__(self):
        rows = db.query("SELECT ms_id, payload FROM raw_moysklad_product")
        self.prods = []
        self.by_msid = {}
        for r in rows:
            p = r["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            code = str(p.get("code") or "")
            name = p.get("name") or ""
            desc = p.get("description") or ""
            blob = (name + " " + desc).strip()
            self.by_msid[r["ms_id"]] = {"code": code, "name": name, "blob": blob}
            if not blob:
                continue
            self.prods.append({"code": code, "text": blob, "brand": _brand(blob)})
        # РОДНЫЕ карточки площадок (приоритетный источник — то, что видит покупатель).
        # Таблицы могут быть пусты, пока не отработали коллекторы — тогда просто фолбэк на МС.
        self.wb_content, self.oz_attr = {}, {}
        for r in db.query("SELECT account, nm_id, payload FROM raw_wb_card_content"):
            self.wb_content[(r["account"], str(r["nm_id"]))] = _wb_card_blob(r["payload"])
        for r in db.query("SELECT account, offer_id, payload FROM raw_ozon_attributes"):
            self.oz_attr[(r["account"], str(r["offer_id"]))] = _oz_attr_blob(r["payload"])

    def _gather(self, offer_code, sell_name, seed_text=None):
        """Собрать описания-родственники. Матч ТОЛЬКО по реальному сигналу: точный код (непустой),
        префикс кода или артикул картриджа из названия. Пустой код + пустые токены → ничего
        (иначе гард по бренду насобирает весь бренд = мусор). seed_text — сама сматченная карточка
        (WB-баркод), гарантированный источник."""
        offer_code = str(offer_code or "")
        pref = _code_prefix(offer_code)
        toks = _cart_tokens(sell_name)
        brand = _brand(sell_name) or _brand(seed_text or "")
        out, seen = [], set()

        def _add(text):
            key = re.sub(r"\s+", " ", (text or "").lower())[:80]
            if text and key not in seen:
                seen.add(key)
                out.append(text)

        if seed_text and seed_text.strip():
            _add(seed_text.strip())
        if not (offer_code or toks):          # нет ни кода, ни артикула — не гадаем
            return brand, toks, out
        for pr in self.prods:
            bl = pr["text"].lower()
            hit = ((offer_code and pr["code"] == offer_code)
                   or (pref and _code_prefix(pr["code"]) == pref)
                   or (toks and any(t in bl for t in toks)))
            if not hit:
                continue
            if brand and pr["brand"] and pr["brand"] != brand:   # гард по бренду
                continue
            _add(pr["text"])
        return brand, toks, out

    @staticmethod
    def _facts(texts):
        t = " | ".join(texts).lower()
        f = {}
        if re.search(r"без\s+чип|чип\s+не\s+треб", t):
            f["chip"] = "без чипа / не требуется"
        elif "с чипом" in t or re.search(r"\bчип\b", t):
            f["chip"] = "с чипом"
        if re.search(r"лазерн|тонер", t):          # лазер/тонер приоритетнее: слово «чернил»
            f["type"] = "тонер/лазерный"           # часто мелькает в тексте лазерных карточек
        elif re.search(r"струйн|чернил", t):
            f["type"] = "струйный/чернила"
        if re.search(r"совместим|аналог", t):
            f["kind"] = "совместимый (аналог)"
        elif "оригинал" in t:
            f["kind"] = "оригинал"
        # ресурс: число 3-5 цифр перед стр/копий/k, не часть кода (не липнет к букве/цифре/слэшу)
        res = sorted({r for r in re.findall(r"(?<![A-Za-z\d/\-])(\d{3,5})\s*(?:стр|копи|k\b|тыс)", t)},
                     key=lambda x: -int(x))
        if res:
            f["resource_pages"] = res[:3]
        if re.search(r"дозаправ|перезаправ|заправляем|многоразов|перезапр", t):
            f["refillable"] = "да (по описанию)"
        # совместимые модели — фразы после «для»
        models, seen = [], set()
        for m in re.findall(r"для\s+([^.,;|]{4,55})", t):
            m = re.sub(r"\s+", " ", m).strip()
            k = m[:30]
            if k not in seen:
                seen.add(k)
                models.append(m)
        if models:
            f["compat"] = models[:5]
        return f

    def _result(self, platform, code, name, texts, brand, toks, **extra):
        res = {"ok": True, "platform": platform, "code": code, "name": name,
               "brand": brand, "tokens": sorted(toks), "n_sources": len(texts),
               "facts": self._facts(texts), "snippets": texts[:6]}
        res.update(extra)
        return res

    def for_ozon(self, sku, account="oz_acc1"):
        r = db.query("SELECT offer_id, name FROM ozon_product WHERE sku=%s AND account=%s",
                     (str(sku), account))
        if not r:
            return {"ok": False, "reason": "sku не найден в ozon_product"}
        offer, name = r[0]["offer_id"], r[0]["name"]
        pcard = self.oz_attr.get((account, str(offer)))          # родная карточка Ozon — приоритет
        brand, toks, texts = self._gather(offer, name, seed_text=pcard)
        via = "ozon_card" if pcard else "offer"
        return self._result("ozon", offer, name, texts, brand, toks, offer_id=offer, via=via)

    def _wb_barcodes(self, nm):
        r = db.query("""SELECT DISTINCT payload->>'barcode' bc FROM raw_wb_report
            WHERE payload->>'nm_id'=%s AND payload->>'barcode' IS NOT NULL""", (str(nm),))
        return [x["bc"] for x in r if x["bc"]]

    def for_wb(self, nm_id, account="wb_acc1"):
        """WB grounding: родная карточка WB (описание+характеристики) — приоритет; плюс баркод→ms_id→
        карточка МС. Фолбэк на заголовок WB для «мёртвого хвоста». Родная карточка даёт факты даже
        когда nm не сшит с МС."""
        pcard = self.wb_content.get((account, str(nm_id)))       # родная карточка WB — приоритет
        # 1) баркодный путь nm → barcode → ms_id → продающая карточка МС (+ родная карточка seed'ом)
        for bc in self._wb_barcodes(nm_id):
            m = db.query("SELECT ms_id FROM ms_barcode WHERE barcode=%s", (bc,))
            if not m:
                continue
            info = self.by_msid.get(m[0]["ms_id"])
            if not info:
                continue
            code, name = info["code"] or "", info["name"] or ""
            seed = " | ".join(x for x in (pcard, info.get("blob")) if x)
            brand, toks, texts = self._gather(code, name, seed_text=seed)
            if texts:
                return self._result("wb", code, name, texts, brand, toks,
                                    via=("wb_card+barcode" if pcard else "barcode"),
                                    barcode=bc, ms_id=m[0]["ms_id"])
        # 2) фолбэк: родная карточка WB и/или её заголовок (nm может быть не в МС)
        c = db.query("SELECT title FROM wb_cards WHERE nm_id=%s AND account=%s", (str(nm_id), account))
        title = c[0]["title"] if c and c[0]["title"] else ""
        if pcard or title:
            brand, toks, texts = self._gather("", title, seed_text=pcard)
            if texts:
                return self._result("wb", "", title or (pcard or "")[:60], texts, brand, toks,
                                    via=("wb_card" if pcard else "wb_title"))
        return {"ok": False, "reason": "nm не сшит с МС и нет родной карточки/заголовка"}


if __name__ == "__main__":
    g = Grounding()
    wb = "--wb" in sys.argv
    ids = [a for a in sys.argv[1:] if not a.startswith("--")]
    if wb:
        ids = ids or [r["item_id"] for r in db.query(
            "SELECT DISTINCT item_id FROM raw_feedback WHERE platform='wb' AND kind='review' "
            "AND item_id IS NOT NULL LIMIT 12")]
    else:
        ids = ids or ["1611110080", "863388173", "652524447", "2509753251"]
    for i in ids:
        res = g.for_wb(i) if wb else g.for_ozon(i)
        print(f"\n=== {'nm' if wb else 'sku'} {i}")
        if not res["ok"]:
            print("  ", res["reason"]); continue
        print(f"  {res['name'][:75]}")
        print(f"  brand={res['brand']} via={res.get('via','offer')} токены={res['tokens']} источников={res['n_sources']}")
        print(f"  ФАКТЫ: {json.dumps(res['facts'], ensure_ascii=False)}")
