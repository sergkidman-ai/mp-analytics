# -*- coding: utf-8 -*-
"""
upd_to_supply.py — конвейер: входящий УПД (XLS/XLSX или ссылка Я.Диска, стандартная 1С-форма)
                   → «Приёмка» (entity/supply) в МойСклад, привязанная к «Заказу поставщику».

Фаза 2. Приёмка создаётся НА ОСНОВАНИИ существующего заказа: товары/кол-во/цены МИГРИРУЮТ из
заказа 1:1 (не подбираем заново из справочника). Из УПД переносим ТОЛЬКО: № и дату входящего
документа и построчно страну происхождения + номер декларации (ГТД). Дополнительно в карточку
товара пишем закупочную цену (buyPrice) и «Код поставщика» (код из УПД).

Запуск:
  python upd_to_supply.py <файл|ссылка Я.Диска> [--create] [--suffix ТЕСТ-]
  По умолчанию — DRY-RUN (разбирает, ищет заказ, считает — но не создаёт и карточки не трогает).
  --create   — реально создать приёмку и обновить карточки товаров.
  --suffix S — задать имя приёмки = S+<номер заказа> (для тестов; в бою имя присваивает МС-нумератор).

Правила (согласованы с заказчиком):
  • Заказ ищем по: контрагент = продавец (ИНН из УПД) И сумма заказа == сумма УПД (с НДС).
  • Ровно один заказ — создаём; 0 или несколько — НЕ создаём, возвращаем кандидатов (stop).
  • Позиции мигрируют из заказа; страну/ГТД проставляем на позицию, сматченную со строкой УПД.
  • Страна/ГТД — точь-в-точь как в УПД: прочерк (--/-) → поле пустое (Китай не подставляем).
  • Услуги (Доставка и пр.) — без страны/ГТД/закупцены.
  • Приёмка: статус «Создан», Проведено=нет, дата = план.дата приёмки заказа, время 08:00.
  • Парсинг колонок — по строке номеров граф ФНС (А|1а|3|9|10|10а|11), не по фикс-индексам.
"""
import os, sys, re, io, zipfile, json, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/mp-analytics")
import invoice_to_po as inv           # fetch, read_grid, meta, get_r, _parse_date, _num, is_delivery
from ms import get, post, put, MS

MSU = MS
STATE_SOZDAN = "2beb6446-a9a3-11f0-0a80-0990001da0b2"   # статус приёмки «Создан»
CODE_ATTR    = "efaefd7a-b130-11ea-0a80-0367000245f4"   # доп.поле товара «Код поставщика» (string)
DASH = {"", "-", "--", "---", "----", "------", "–", "—"}   # варианты прочерка

meta = inv.meta


# ═══════════════════════ 0. Чтение файла (с фиксом xlsx SharedStrings) ═════════
def read_grid_safe(path):
    """inv.read_grid + починка xlsx, где sharedStrings.xml назван с заглавной S (не-Excel генератор)."""
    try:
        return inv.read_grid(path)
    except KeyError:
        if open(path, "rb").read(2) != b"PK":
            raise
        buf = io.BytesIO()
        with zipfile.ZipFile(path) as z:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as o:
                for it in z.infolist():
                    data = z.read(it.filename)
                    name = it.filename
                    if name.lower() == "xl/sharedstrings.xml":
                        name = "xl/sharedStrings.xml"
                        data = data.replace(b"SharedStrings.xml", b"sharedStrings.xml")
                    o.writestr(name, data)
        fixed = path + ".fixed.xlsx"
        open(fixed, "wb").write(buf.getvalue())
        return inv.read_grid(fixed)


# ═══════════════════════ 1. Разбор УПД (якорь — номера граф ФНС / метки) ════════
def _s(v):
    return "" if v in ("", None) else str(v).strip()


def _norm(v):
    return re.sub(r"[\s\-]+", "", str(v or "")).lower()


def _find_graph_row(rows):
    """Строка номеров граф 1С-УПД с подграфами: маркеры '1а','10а','11'. → (r, {граф: колонка})."""
    for r, row in enumerate(rows):
        st = set(_s(v) for v in row)
        if "1а" in st and "10а" in st and "11" in st:
            cols = {}
            for c, v in enumerate(_s(x) for x in row):
                if v and v not in cols:          # первое вхождение графы
                    cols[v] = c
            return r, cols
    return None, None


def _columns_by_labels(rows):
    """Fallback: колонки по текстам заголовков (шаблоны без подграф-букв 1а/10а)."""
    hdr = None
    for r, row in enumerate(rows):
        j = _norm(" ".join(_s(v) for v in row))
        if "наименованиетовар" in j and "странапроисхожд" in j and "регистрационн" in j:
            hdr = r; break
    if hdr is None:
        return None
    cmap = {}; stoim = []
    for c, v in enumerate(rows[hdr]):
        n = _norm(v)
        if not n:
            continue
        if "кодтовара" in n and "kod" not in cmap:            cmap["kod"] = c
        elif ("п/п" in n or n.startswith("№")) and "num" not in cmap: cmap["num"] = c
        elif "наименованиетовар" in n and "name" not in cmap: cmap["name"] = c
        elif "количество" in n and "qty" not in cmap:         cmap["qty"] = c
        elif "стоимостьтоваров" in n:                          stoim.append(c)   # гр.5 (без) и гр.9 (с НДС)
        elif "регистрационн" in n and "gtd" not in cmap:      cmap["gtd"] = c
    if stoim:
        cmap["sum_vat"] = max(stoim)          # «с налогом» — правый столбец «Стоимость товаров»
    for r in range(hdr, min(hdr + 4, len(rows))):    # подшапка страны: цифровой код / краткое наименование
        for c, v in enumerate(rows[r]):
            n = _norm(v)
            if "цифровой" in n and "country_code" not in cmap: cmap["country_code"] = c
            if "краткое" in n and "country_name" not in cmap:  cmap["country_name"] = c
    return cmap


def _columns(rows):
    """Карта {ключ: колонка}. Приоритет — строка граф с подграфами; иначе — по меткам."""
    gr, cols = _find_graph_row(rows)
    if gr is not None:
        need = {"А": "kod", "1а": "name", "1": "num", "3": "qty", "9": "sum_vat",
                "10": "country_code", "10а": "country_name", "11": "gtd"}
        cmap = {k: cols[g] for g, k in need.items() if g in cols}
        if all(k in cmap for k in ("name", "qty", "sum_vat")):
            return cmap
    return _columns_by_labels(rows)


def _label_row(rows, label):
    """Значение справа от ячейки-метки label (первое непустое в той же строке)."""
    lab = label.lower()
    for row in rows:
        for c, v in enumerate(row):
            if v and lab in str(v).lower():
                for v2 in row[c + 1:]:
                    if _s(v2):
                        return _s(v2), list(row), c
    return None, None, None


def _inn(val):
    m = re.search(r"\b(\d{10,12})\b", val or "")
    return m.group(1) if m else None


def parse_upd(rows):
    """→ dict(seller_inn, buyer_inn, number, date, positions[...])."""
    cmap = _columns(rows)
    if not cmap:
        raise ValueError("Не найдена ни строка номеров граф, ни строка-заголовок УПД — "
                         "это не распознаваемый шаблон. Нужен отдельный адаптер.")
    for must in ("name", "qty", "sum_vat"):
        if must not in cmap:
            raise ValueError(f"Не определена колонка для '{must}' — нераспознанный шаблон УПД")

    def cell(row, key):
        c = cmap.get(key)
        return _s(row[c]) if (c is not None and c < len(row)) else ""

    seller = _inn((_label_row(rows, "ИНН/КПП продавца")[0]) or "")
    buyer  = _inn((_label_row(rows, "ИНН/КПП покупателя")[0]) or "")

    # № и дата УПД — из строки «Счет-фактура №»
    num = None; dt = None
    for row in rows:
        joined = " ".join(_s(v) for v in row)
        if re.search(r"Счет-фактура\s*№", joined, re.I):
            cells = [_s(v) for v in row if _s(v)]
            after = []
            for i, v in enumerate(cells):
                if re.search(r"Счет-фактура", v, re.I):
                    after = cells[i + 1:]; break
            for v in after:
                if num is None and re.match(r"^[\wА-Яа-я/№.\-]+$", v) and v.lower() != "от" and "исправл" not in v.lower():
                    num = v.lstrip("№ ").strip()
                try:
                    dt = inv._parse_date(v); break
                except (Exception, SystemExit):
                    pass
            if num or dt:
                break

    positions = []
    for row in rows:
        vals = set(_s(v) for v in row)
        if "1а" in vals and "11" in vals:          # повторная строка номеров граф (мультистраничный УПД)
            continue
        name = cell(row, "name")
        if not name or not re.search(r"[A-Za-zА-Яа-я]", name):   # служебные/граф-строки без букв в наимен.
            continue
        if re.match(r"^\d+[абаb]?$", name.strip()):  # граф-номер, просочившийся в колонку наименования («1а»)
            continue
        joined = " ".join(_s(v) for v in row).lower()
        if any(k in joined for k in ("всего к оплате", "итого", "страница", "документ составлен",
                                     "наименование товара")):
            continue
        qty = inv._num(cell(row, "qty")); sv = inv._num(cell(row, "sum_vat"))
        if qty is None or sv is None:      # у товарной строки кол-во и сумма — числа
            continue
        num_pp = cell(row, "num")
        cc = cell(row, "country_code"); cname = cell(row, "country_name"); gtd = cell(row, "gtd")
        has_country = cname not in DASH and re.search(r"\d", cc or "")
        positions.append({
            "num": int(float(num_pp.replace(",", "."))) if re.match(r"^\d+([.,]0+)?$", num_pp or "") else len(positions) + 1,
            "kod": cell(row, "kod"),
            "name": name,
            "qty": qty,
            "sum_vat": sv,
            "country": cname if has_country else None,
            "gtd": gtd if gtd not in DASH else None,
        })
    return {"seller_inn": seller, "buyer_inn": buyer, "number": num, "date": dt, "positions": positions}


# ═══════════════════════ 1b. Разбор УПД из XML ЭДО (формат ФНС ON_NSCHFDOPPR) ═══
def upd_xml_bytes(path):
    """Если файл — УПД-XML ФНС (голый .xml или zip выгрузки Диадока «в исходном формате»),
    вернуть bytes самого XML (титул продавца ON_NSCHFDOPPR_*.xml); иначе None (→ путь Excel)."""
    raw = open(path, "rb").read()
    if raw[:5] == b"<?xml" or raw[:1] == b"<":
        return raw
    if raw[:2] == b"PK":                      # zip: xlsx ИЛИ пакет Диадока
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    low = n.lower()
                    if low.endswith(".xml") and ("nschfdoppr" in low or low.startswith("on_")):
                        return z.read(n)      # найден титул УПД — это пакет Диадока
        except Exception:
            return None
    return None                                # не xlsx-структура УПД → пусть идёт как Excel


def _inn_of(node):
    """ИНН из блока СвПрод/СвПокуп (ЮЛ → ИННЮЛ, ИП → ИННФЛ)."""
    if node is None:
        return None
    idsv = node.find("ИдСв")
    if idsv is None:
        return None
    yl = idsv.find("СвЮЛУч")
    if yl is not None and yl.get("ИННЮЛ"):
        return yl.get("ИННЮЛ")
    ip = idsv.find("СвИП")
    if ip is not None and ip.get("ИННФЛ"):
        return ip.get("ИННФЛ")
    return None


def parse_upd_xml(raw):
    """bytes XML УПД (ФНС ON_NSCHFDOPPR) → dict(seller_inn, buyer_inn, number, date, positions[...]).
    Формат совместим с parse_upd (Excel): позиции — тот же набор ключей."""
    m = re.search(rb'encoding="([^"]+)"', raw[:120])
    enc = (m.group(1).decode("ascii", "replace") if m else "utf-8")
    root = ET.fromstring(raw.decode(enc, "replace"))
    doc = root.find("Документ")
    if doc is None:
        raise ValueError("XML не является УПД ФНС (нет узла <Документ>)")
    sf = doc.find("СвСчФакт")
    if sf is None:
        raise ValueError("XML УПД без <СвСчФакт> — нераспознанный документ")
    seller = _inn_of(sf.find("СвПрод"))
    buyer  = _inn_of(sf.find("СвПокуп"))
    number = sf.get("НомерДок")
    dt = None
    ds = sf.get("ДатаДок")                      # ДД.ММ.ГГГГ
    if ds and re.match(r"\d{2}\.\d{2}\.\d{4}", ds):
        d, mo, y = ds[:10].split("."); dt = date(int(y), int(mo), int(d))

    positions = []
    tab = doc.find("ТаблСчФакт")
    for st in (tab.findall("СведТов") if tab is not None else []):
        svdt = st.find("СвДТ")                  # сведения о таможенной декларации (только для импорта)
        cc  = svdt.get("КодПроисх") if svdt is not None else None   # ОКСМ цифровой код страны
        gtd = svdt.get("НомерДТ")   if svdt is not None else None   # номер ГТД/ДТ
        dop = st.find("ДопСведТов")
        kod = (dop.get("КодТов") if dop is not None else "") or ""  # артикул/код поставщика
        cname = _country_name_by_code(cc) if cc else None
        positions.append({
            "num": int(float(st.get("НомСтр"))) if (st.get("НомСтр") or "").strip() else len(positions) + 1,
            "kod": kod,
            "name": st.get("НаимТов") or "",
            "qty": inv._num(st.get("КолТов")),
            "sum_vat": inv._num(st.get("СтТовУчНал")),      # стоимость с НДС (гр.9)
            "country": cname,
            "gtd": gtd if (gtd and gtd not in DASH) else None,
        })
    return {"seller_inn": seller, "buyer_inn": buyer, "number": number, "date": dt, "positions": positions}


# ═══════════════════════ 2. Справочник стран (кэш) ═════════════════════════════
_CC = None
_CC_CODE = None
def _country_id(name):
    global _CC
    if _CC is None:
        _CC = {}
        for row in inv.get_r("/entity/country?limit=1000").get("rows", []):
            _CC[(row.get("name") or "").strip().lower()] = row["id"]
    return _CC.get((name or "").strip().lower())


def _country_name_by_code(code):
    """ОКСМ цифровой код (напр. '156') → наименование страны из справочника МС ('Китай')."""
    global _CC_CODE
    if _CC_CODE is None:
        _CC_CODE = {}
        for row in inv.get_r("/entity/country?limit=1000").get("rows", []):
            c = (row.get("code") or "").strip()
            if c:
                _CC_CODE[c] = (row.get("name") or "").strip()
    return _CC_CODE.get((code or "").strip())


# ═══════════════════════ 3. Поиск заказа поставщику ════════════════════════════
def find_order(seller_inn, total_kop, upd_date):
    """Заказы контрагента (ИНН продавца) с sum==total_kop. → (order|None, candidates[], agent)."""
    cps = inv.get_r(f"/entity/counterparty?filter=inn={seller_inn}&limit=5").get("rows", [])
    if not cps:
        return None, [], None
    agent = cps[0]
    href = f"{MSU}/entity/counterparty/{agent['id']}"
    flt = urllib.parse.quote(f"agent={href}", safe="=")
    order_q = urllib.parse.quote("moment,desc")
    hits = []
    for off in range(0, 600, 100):
        rows = inv.get_r(f"/entity/purchaseorder?filter={flt}&order={order_q}&limit=100&offset={off}"
                         f"&expand=organization,agent,store").get("rows", [])
        if not rows:
            break
        for o in rows:
            if o.get("sum") == total_kop:
                hits.append(o)
        last = rows[-1].get("moment", "")[:10]
        if upd_date and last and last < _month_back(upd_date, 2):
            break
    return (hits[0] if len(hits) == 1 else None), hits, agent


def _month_back(d, n):
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12; y -= 1
    return date(y, m, 1).isoformat()


# ═══════════════════════ 4. Матчинг строки УПД ↔ позиции заказа ════════════════
def _tok_strong(name):
    """Сильные артикульные токены: CS-/GG-/CR-/XX-… — редко совпадают у разных товаров."""
    return set(re.findall(r"\b[A-Z]{2,}-[A-Z0-9./]+", (name or "").upper()))


def _tok_weak(name):
    """Слабые токены: буквенно-цифровые ≥4 с цифрой (модели, ресурс) — могут совпадать."""
    return {t for t in re.findall(r"\b[A-ZА-Я0-9./-]{4,}\b", (name or "").upper()) if re.search(r"\d", t)}


def match_rows(order_pos, upd_pos):
    """Для каждой позиции заказа найти строку УПД. → {order_index: upd_row|None}, warns[]."""
    res = {}; warns = []; used = set()

    def take(i, ui):
        res[i] = upd_pos[ui]; used.add(ui)

    def best(i, tokfn):
        """Строка УПД с уникально-максимальным пересечением токенов (различающий код перевешивает)."""
        ptok = tokfn(order_pos[i]["assortment"].get("name", ""))
        if not ptok:
            return None
        scored = sorted(((len(ptok & tokfn(u["name"])), ui)
                         for ui, u in enumerate(upd_pos) if ui not in used and (ptok & tokfn(u["name"]))),
                        reverse=True)
        if not scored:
            return None
        if len(scored) == 1 or scored[0][0] > scored[1][0]:   # единственный максимум
            return scored[0][1]
        return None

    # 1) по артикулу/коду товара == код [1] УПД
    for i, p in enumerate(order_pos):
        a = p["assortment"]
        keys = {str(a.get("article") or "").strip().upper(), str(a.get("code") or "").strip().upper()} - {""}
        for ui, u in enumerate(upd_pos):
            if ui not in used and u["kod"] and u["kod"].strip().upper() in keys:
                take(i, ui); break
    # 2) по сильному артикульному токену (уникальный максимум пересечения)
    for i in range(len(order_pos)):
        if i not in res:
            ui = best(i, _tok_strong)
            if ui is not None:
                take(i, ui)
    # 3) по слабому токену (уникальный максимум пересечения — различает близнецов по коду модели)
    for i in range(len(order_pos)):
        if i not in res:
            ui = best(i, _tok_weak)
            if ui is not None:
                take(i, ui)
    # 4) по вхождению наименования — последний шанс
    for i, p in enumerate(order_pos):
        if i in res:
            continue
        pn = (p["assortment"].get("name", "") or "").lower()
        for ui, u in enumerate(upd_pos):
            if ui in used:
                continue
            un = (u["name"] or "").lower()
            if un[:18] and (un[:18] in pn or pn[:18] in un):
                take(i, ui); break
    for i in range(len(order_pos)):
        res.setdefault(i, None)
    return res, warns


# ═══════════════════════ 5. Основной конвейер ═════════════════════════════════
def process(src, create=True, suffix=""):
    res = {"ok": False, "created": False, "stop": False, "error": None, "warns": []}
    try:
        path = inv.fetch(src)
        xml = upd_xml_bytes(path)
        if xml is not None:                       # УПД из ЭДО (Диадок): XML ФНС / zip выгрузки
            upd = parse_upd_xml(xml)
        else:                                     # прежний путь: Excel-УПД (1С-форма)
            kind, payload = read_grid_safe(path)
            if kind != "table":
                raise ValueError("Ожидался Excel-УПД (xls/xlsx) или XML ЭДО (ФНС), "
                                 "получен PDF/иное — нужен отдельный адаптер.")
            upd = parse_upd(payload)
        res["upd"] = {"number": upd["number"], "date": upd["date"].isoformat() if upd["date"] else None,
                      "seller_inn": upd["seller_inn"], "buyer_inn": upd["buyer_inn"],
                      "positions": len(upd["positions"])}
        if not upd["seller_inn"]:
            raise ValueError("Не распознан ИНН продавца в УПД")
        if not upd["positions"]:
            raise ValueError("Не распознано ни одной товарной строки")

        total = round(sum(p["sum_vat"] or 0 for p in upd["positions"]), 2)
        total_kop = int(round(total * 100))
        res["total"] = total

        order, cands, agent = find_order(upd["seller_inn"], total_kop, upd["date"])
        res["candidates"] = [{"name": o["name"], "sum": o["sum"] / 100, "date": o.get("moment", "")[:10]}
                             for o in cands]
        if order is None:
            res["stop"] = True
            res["error"] = ("Заказ не найден по сумме" if not cands
                            else f"Неоднозначно: {len(cands)} заказов с суммой {total}")
            return res

        res["order"] = {"name": order["name"], "id": order["id"], "sum": order["sum"] / 100,
                        "plan": (order.get("deliveryPlannedMoment") or "")[:10]}
        oid = order["id"]
        opos = inv.get_r(f"/entity/purchaseorder/{oid}/positions?expand=assortment&limit=200").get("rows", [])
        mp, warns = match_rows(opos, upd["positions"])
        res["warns"] += warns

        # план-дата приёмки → moment 08:00; иначе дата УПД
        plan = (order.get("deliveryPlannedMoment") or "").split()[0] or \
               (upd["date"].isoformat() if upd["date"] else None)
        moment = f"{plan} 08:00:00"
        inc_date = upd["date"].isoformat() if upd["date"] else plan

        sup_pos = []; c_set = g_set = bp = code_set = 0; matched = 0; unmatched = []
        for i, p in enumerate(opos):
            a = p["assortment"]; u = mp.get(i)
            is_prod = a["meta"]["type"] == "product"
            row = {"quantity": p["quantity"], "price": p["price"], "vat": p.get("vat", 22),
                   "vatEnabled": p.get("vatEnabled", True), "discount": p.get("discount", 0),
                   "assortment": {"meta": a["meta"]}}
            if u:
                matched += 1
                if u["country"]:
                    cid = _country_id(u["country"])
                    if cid:
                        row["country"] = meta("country", cid); c_set += 1
                    else:
                        res["warns"].append(f"страна «{u['country']}» не найдена в справочнике — пропущена")
                if u["gtd"]:
                    row["gtd"] = {"name": u["gtd"]}; g_set += 1
            else:
                unmatched.append(a.get("name", "")[:40])
            sup_pos.append(row)

            # карточка товара: buyPrice + «Код поставщика» (только для товаров, при --create)
            if create and is_prod:
                pid = a["meta"]["href"].rstrip("/").split("/")[-1]
                cur = inv.get_r(f"/entity/product/{pid}")
                body = {}
                cm = cur.get("buyPrice", {}).get("currency", {}).get("meta")
                body["buyPrice"] = {"value": p["price"], **({"currency": {"meta": cm}} if cm else {})}
                bp += 1
                if u and u["kod"]:
                    body["attributes"] = [{
                        "meta": {"href": f"{MSU}/entity/product/metadata/attributes/{CODE_ATTR}",
                                 "type": "attributemetadata", "mediaType": "application/json"},
                        "value": u["kod"]}]
                    code_set += 1
                put(f"/entity/product/{pid}", body)

        if unmatched:
            res["warns"].append(f"без матча со строкой УПД (страна/ГТД не проставлены): {unmatched}")
        res["stats"] = {"positions": len(sup_pos), "matched": matched, "country": c_set,
                        "gtd": g_set, "buyPrice": bp, "code": code_set}

        payload_supply = {
            "organization": meta("organization", order["organization"]["id"]),
            "agent": meta("counterparty", order["agent"]["id"], "counterparty"),
            "store": meta("store", order["store"]["id"]),
            "purchaseOrder": meta("purchaseorder", oid),
            "incomingNumber": upd["number"] or "",
            "incomingDate": f"{inc_date} 00:00:00",
            "moment": moment, "applicable": False,
            "vatEnabled": True, "vatIncluded": True,
            "state": meta("supply/metadata/states", STATE_SOZDAN, "state"),
            "description": (f"УПД № {upd['number']} от {inc_date} на осн. заказа {order['name']}. "
                            f"Страна/ГТД перенесены построчно из УПД."),
            "positions": sup_pos,
        }
        if suffix:
            payload_supply["name"] = f"{suffix}{order['name']}"

        if not create:
            res["ok"] = True
            res["dry"] = True
            return res

        st, resp = post("/entity/supply", payload_supply)
        if st in (200, 201):
            res["ok"] = True; res["created"] = True
            res["supply"] = {"name": resp.get("name"), "id": resp["id"], "sum": resp.get("sum", 0) / 100}
            res["url"] = "https://online.moysklad.ru/app/#supply/edit?id=" + resp["id"]
        else:
            res["error"] = f"HTTP {st}: {json.dumps(resp, ensure_ascii=False)[:300]}"
        return res
    except SystemExit as e:
        res["error"] = str(e); return res
    except Exception as e:
        import traceback
        res["error"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc()[-800:]
        return res


# ═══════════════════════ 6. Отчёт ═════════════════════════════════════════════
def format_report(res):
    L = []
    u = res.get("upd", {})
    if res.get("error") and not res.get("stop"):
        L.append(f"❌ Ошибка: {res['error']}")
        if u:
            L.append(f"УПД № {u.get('number')} от {u.get('date')} | продавец ИНН {u.get('seller_inn')}")
        return "\n".join(L)

    L.append(f"📦 УПД № {u.get('number')} от {u.get('date')} | продавец ИНН {u.get('seller_inn')} | строк: {u.get('positions')}")
    L.append(f"Сумма с НДС: {res.get('total')}")

    if res.get("stop"):
        L.append(f"\n⛔ Приёмка не создана — {res['error']}.")
        if res.get("candidates"):
            L.append("Кандидаты-заказы (проверьте вручную):")
            for c in res["candidates"]:
                L.append(f"  • {c['name']} — {c['sum']} ₽ от {c['date']}")
        else:
            L.append("Заказов с такой суммой у поставщика не найдено.")
        return "\n".join(L)

    o = res.get("order", {})
    L.append(f"Заказ-основание: {o.get('name')} (сумма {o.get('sum')}, план приёмки {o.get('plan')})")
    s = res.get("stats", {})
    L.append(f"Позиции: {s.get('matched')}/{s.get('positions')} сматчено | "
             f"страна: {s.get('country')} | ГТД: {s.get('gtd')} | buyPrice: {s.get('buyPrice')} | код: {s.get('code')}")
    for w in res.get("warns", []):
        L.append(f"⚠ {w}")
    if res.get("dry"):
        L.append("\n[DRY-RUN] Приёмка НЕ создана, карточки не изменены. Запустите с --create.")
    elif res.get("created"):
        sup = res["supply"]
        L.append(f"\n✅ Создана приёмка «{sup['name']}» на сумму {sup['sum']}")
        L.append(res.get("url", ""))
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("Использование: python upd_to_supply.py <файл|ссылка> [--create] [--suffix S]")
        return
    src = args[0]
    create = "--create" in args
    suffix = ""
    if "--suffix" in args:
        i = args.index("--suffix")
        if i + 1 < len(args):
            suffix = args[i + 1]
    res = process(src, create=create, suffix=suffix)
    print(format_report(res))
    if res.get("trace"):
        print("\n--- trace ---\n" + res["trace"])


if __name__ == "__main__":
    main()
