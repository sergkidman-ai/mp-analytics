# поток: rev
"""reports/sku_relations.py — связи номенклатуры: chip_pair | drum_toner | kit_component | color_pair.

ЗАЧЕМ. Покупатель спрашивает про соседний лот, а не про тот, на котором стоит: «а с чипом есть?»
(стоит на бесчиповом), «что входит в комплект?», «а тонер к этому барабану?». Раньше ответ на такое
собирался из общих слов или из знания модели — то есть выдумывался. Теперь связь берётся из НАШЕЙ
номенклатуры и подставляется вместе со ссылкой, а если связи нет — не подставляется ничего.

ПРИНЦИП. Строится детерминированно, без LLM, из того, что уже собрано: индекс совместимости
`compat_index` (позиция + наш артикул + название + ссылка + тип товара + модели принтеров) плюс
атрибуты/описания карточек (`raw_ozon_attributes`, `raw_wb_card_content`, `raw_yandex_offer`) —
оттуда состояние чипа.

Связь ВСЕГДА внутри одного канала покупателя — пары «площадка + аккаунт», как в
`catalog._same_channel`: у нас два магазина на Ozon и два на ВБ, и лот Премиума покупателю Дисквэра
не поможет — это другой продавец.

ТИПЫ СВЯЗЕЙ
  chip_pair      бесчиповый лот → тот же код расходника с чипом («артикул_от» всегда без чипа:
                 связь нужна ровно в одну сторону — предложить версию с чипом тому, кому чип нужен).
  drum_toner     фотобарабан ↔ тонер-картридж под одну серию принтера. Два правила: код-семейство
                 (DR-2085 ↔ TN-2085, DK-1150 ↔ TK-1150 — те же цифры, барабанный префикс против
                 тонерного) и пересечение списков совместимости (общие модели принтеров).
  kit_component  комплект ↔ его составные: комплект узнаём по названию («комплект», «набор», «+»,
                 «(N шт.)»), составные — по кодам расходника внутри названия комплекта.
  color_pair     струйные: чёрный ↔ цветной. Canon по номеру пары (PG-510 ↔ CL-511), HP по одному
                 номеру на обе стороны (122, 123, 652, 653), Epson по серии кода; карточки без
                 кода в названии («Картридж для Canon Pixma MP492») — по общей серии принтеров.

Ссылка и идентификатор для покупателя формируются один раз, при сборке (`catalog._plat_ref`), и
лежат в таблице — ответу остаётся подставить готовое.

    ./venv/bin/python -m reports.sku_relations --build           # пересобрать
    ./venv/bin/python -m reports.sku_relations --stats           # размер по типам и аккаунтам
    ./venv/bin/python -m reports.sku_relations --check 3896      # связи по нашему артикулу
"""
import os
import re
import sys
import pathlib
import collections

import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                        # noqa: E402
from reports.card_facts import facts_ozon, facts_wb, _classify_chip, _cart_code   # noqa: E402

TYPES = ("chip_pair", "drum_toner", "kit_component", "color_pair")
MAX_PER_LINK = 3          # больше трёх соседей одного типа покупателю не нужно, а таблице вредно

# ─────────────────────────── код расходника по нашей конвенции ───────────────────────────
# Наши названия строятся одинаково: «Картридж W1360A для принтеров HP …», «Фотобарабан DR-2085 DU
# для принтеров Brother …», «Комплект картриджей W1360A (10 шт.) …», у Дисквэра с приставкой DS.
# Поэтому код надёжнее брать из головы названия, чем общим шаблоном: у Canon он вообще без букв
# («Картриджи 067H для принтеров…»), и никакой универсальный шаблон его не поймает.
_HEAD_RX = re.compile(
    r"^\s*(?:комплект\w*\s+)?"
    r"(?:картридж\w*|тонер-?картридж\w*|тонер|фотобарабан\w*|драм-?картридж\w*|барабан\w*|"
    r"блок\s+фотобарабана|чернил\w+|печатающ\w+\s+головк\w+|лент\w+)\s+"
    r"(?:DS\s+)?"
    r"(?:HP|Canon|Kyocera|Brother|Xerox|Samsung|Epson|Ricoh|Pantum|OKI|Lexmark|Sharp|Konica)?\s*"
    r"(?:№\s*)?([0-9A-Z][0-9A-Z\-/.]{1,15})\b", re.I)
_CODE_STOP = {"ДЛЯ", "DS", "НА", "К", "С", "И", "ШТ"}
# все коды, встречающиеся в названии комплекта: «Картридж CF400A + CF401A + CF402A», «KP-108IN/KP-36IP»
_ANY_CODE_RX = re.compile(r"(?<![0-9A-Z])((?:[A-Z]{1,4}-?)?\d{2,5}[A-Z]{0,3})(?![0-9A-Z])")
_KIT_RX = re.compile(r"комплект|набор|\+|\(\s*\d+\s*шт", re.I)
# барабанные и тонерные префиксы одного семейства: у Brother DR-2085 идёт в пару к TN-2085,
# у Kyocera DK-1150 — к TK-1150. Цифры те же, меняется первая буква.
_DRUM_PFX = {"DR", "DK", "DU"}
_TONER_PFX = {"TN", "TK"}
_PFX_RX = re.compile(r"^([A-Z]{2})-?(\d{2,5})([A-Z]*)$")


def code_of_title(title):
    """Код расходника из названия лота ('' → None). Сначала наша конвенция, потом общий шаблон."""
    t = re.sub(r"\s+", " ", title or "").strip()
    m = _HEAD_RX.match(t)
    if m:
        c = m.group(1).upper().strip("-/.")
        if c not in _CODE_STOP and re.search(r"\d", c):
            return c
    return _cart_code(t)


def codes_in_title(title):
    """Все коды расходника внутри названия — состав комплекта («… W1360A (10 шт.) …»)."""
    t = re.sub(r"\s+", " ", title or "")
    # хвост «на 11500 страниц», «(10 шт.)» — это ресурс и количество, не коды
    t = re.sub(r"\(\s*\d+\s*шт[^)]*\)|на\s+\d+\s*(?:страниц|стр)\w*", " ", t, flags=re.I)
    out, seen = [], set()
    for m in _ANY_CODE_RX.finditer(t.upper()):
        c = m.group(1)
        if c in seen or c in _CODE_STOP:
            continue
        seen.add(c)
        out.append(c)
    return out[:6]


def _family(code):
    """(семейство, цифры) для пары барабан/тонер: DR-2085 → ('drum','2085'), TK-1150 → ('toner','1150')."""
    m = _PFX_RX.match((code or "").upper().replace(" ", ""))
    if not m:
        return None, None
    pfx, num = m.group(1), m.group(2)
    if pfx in _DRUM_PFX:
        return "drum", num
    if pfx in _TONER_PFX:
        return "toner", num
    return None, None


# ─────────────────────────────── позиции номенклатуры ───────────────────────────────

def _spine():
    """Позиции из индекса совместимости: канал, id для покупателя, наш артикул, название, тип."""
    rows = db.query("""SELECT platform, account, item_id,
                              max(article) article, max(title) title, max(url) url,
                              max(item_kind) item_kind, max(brand) brand
                         FROM compat_index WHERE verdict='yes'
                        GROUP BY platform, account, item_id""")
    return {(r["platform"], r["account"], str(r["item_id"])): {
        "platform": r["platform"], "account": r["account"], "item_id": str(r["item_id"]),
        "article": r["article"], "title": r["title"], "url": r["url"],
        "kind": r["item_kind"], "brand": r["brand"], "chip": None, "code": None} for r in rows}


def _chip_from_cards(items, verbose=False):
    """Состояние чипа из карточек: Ozon — атрибуты/rich, ВБ — описание и характеристики,
    Яндекс — описание офера. Плюс само название на всех площадках: «Картридж … без чипа» пишут
    прямо в заголовке, а в атрибуты это не попадает."""
    by_oz, by_wb, by_ya = {}, {}, {}
    for v in items.values():
        if v["platform"] == "ozon":
            by_oz.setdefault((v["account"], str(v["article"] or "")), []).append(v)
        elif v["platform"] == "wb":
            by_wb.setdefault(str(v["item_id"]), []).append(v)
        else:
            by_ya.setdefault((v["account"], str(v["article"] or "")), []).append(v)

    def _apply(tgt, chip, code):
        for v in tgt:
            if chip:
                v["chip"] = chip
            if code and not v["code"]:
                v["code"] = code

    n = 0
    for off in range(0, 200000, 2000):
        rows = db.query("SELECT account, offer_id, payload FROM raw_ozon_attributes "
                        "ORDER BY offer_id OFFSET %s LIMIT 2000", (off,))
        if not rows:
            break
        for r in rows:
            tgt = by_oz.get((r["account"], str(r["offer_id"])))
            if not tgt:
                continue
            f = facts_ozon(r["payload"]) or {}
            _apply(tgt, f.get("chip"), f.get("code"))
            n += len(tgt)
    if verbose:
        print(f"  чип из карточек Ozon: {n} позиций")

    n = 0
    for off in range(0, 200000, 2000):
        rows = db.query("SELECT nm_id, payload FROM raw_wb_card_content "
                        "ORDER BY nm_id OFFSET %s LIMIT 2000", (off,))
        if not rows:
            break
        for r in rows:
            tgt = by_wb.get(str(r["nm_id"]))
            if not tgt:
                continue
            f = facts_wb(r["payload"]) or {}
            _apply(tgt, f.get("chip"), f.get("code"))
            n += len(tgt)
    if verbose:
        print(f"  чип из карточек ВБ: {n} позиций")

    n = 0
    for off in range(0, 200000, 2000):
        rows = db.query("SELECT account, offer_id, payload FROM raw_yandex_offer "
                        "ORDER BY offer_id OFFSET %s LIMIT 2000", (off,))
        if not rows:
            break
        for r in rows:
            tgt = by_ya.get((r["account"], str(r["offer_id"])))
            if not tgt:
                continue
            o = (r["payload"] or {}).get("offer") or {}
            _apply(tgt, _classify_chip(" || ".join([str(o.get("name") or ""),
                                                    str(o.get("description") or "")])), None)
            n += len(tgt)
    if verbose:
        print(f"  чип из оферов Маркета: {n} позиций")

    for v in items.values():
        by_name = _classify_chip(v["title"])
        # «без чипа» в названии сильнее любого описания: описание у нас шаблонное и говорит
        # «полностью готов к печати» даже там, где чипа нет
        if by_name == "none" or (by_name and not v["chip"]):
            v["chip"] = by_name
        if not v["code"]:
            v["code"] = code_of_title(v["title"])


def load_items(verbose=False):
    """Позиции номенклатуры с кодом расходника и состоянием чипа."""
    items = _spine()
    if verbose:
        print(f"  позиций в номенклатуре: {len(items)}")
    _chip_from_cards(items, verbose=verbose)
    return items


# ─────────────────────────────────── правила связей ───────────────────────────────────

def _ref(v):
    """Идентификатор и ссылка для покупателя — тем же способом, что и в каталоге."""
    from reports.catalog import _plat_ref
    return _plat_ref({"platform": v["platform"], "id": v["item_id"],
                      "article": v["article"], "url": v["url"]})


def _row(src, dst, rel_type, basis, rank):
    ref, url = _ref(dst)
    return (src["platform"], src["account"], rel_type, src["item_id"], dst["item_id"],
            src["article"], dst["article"], src["kind"], dst["kind"],
            (dst["title"] or "")[:200], url, ref, basis, rank)


def _by_code(items, kinds=None):
    g = collections.defaultdict(list)
    for v in items.values():
        if v["code"] and (kinds is None or v["kind"] in kinds):
            g[(v["platform"], v["account"], v["code"])].append(v)
    return g


def chip_pairs(items):
    """Бесчиповый лот → тот же код расходника с чипом, тот же канал и тот же тип товара.

    Направление одно. Обратная связь («у вас есть без чипа?») смысла в ответе не имеет: чип нужен,
    чтобы принтер опознал картридж, и предлагать вместо готового лота бесчиповый — вредный совет."""
    out = []
    for key, vs in _by_code(items).items():
        chipless = [v for v in vs if v["chip"] == "none"]
        if not chipless:
            continue
        for src in chipless:
            cand = [v for v in vs if v["chip"] == "installed" and v["kind"] == src["kind"]
                    and v["item_id"] != src["item_id"]]
            # ровно тот же товар, только с чипом: сначала одиночные лоты, потом всё остальное
            cand.sort(key=lambda v: (v["kind"] == "kit", v["title"] or ""))
            for i, dst in enumerate(cand[:MAX_PER_LINK], 1):
                out.append(_row(src, dst, "chip_pair", f"code={key[2]}", i))
    return out


def _drum_toner_by_code(items):
    """Правило кода: DR-2085 ↔ TN-2085, DK-1150 ↔ TK-1150 — одно семейство, те же цифры."""
    fam = collections.defaultdict(lambda: {"drum": [], "toner": []})
    for v in items.values():
        side, num = _family(v["code"])
        if side:
            fam[(v["platform"], v["account"], num)][side].append(v)
    out = []
    for key, sides in fam.items():
        for a, b in (("drum", "toner"), ("toner", "drum")):
            # спросили «а тонер к этому барабану?» — показываем ОДИНОЧНЫЙ картридж, а не комплект
            # из шести: комплект уместен, когда про него спросили, а не как ответ по умолчанию
            cand = sorted(sides[b], key=lambda v: (v["kind"] == "kit",
                                                   bool(_KIT_RX.search(v["title"] or "")),
                                                   v["title"] or ""))
            for src in sides[a]:
                for i, dst in enumerate(cand[:MAX_PER_LINK], 1):
                    out.append(_row(src, dst, "drum_toner",
                                    f"code={src['code']}~{dst['code']}", i))
    return out


def _drum_toner_by_models(items, have, verbose=False):
    """Правило совместимости: у барабана и тонера общая серия принтеров. Берём для каждого барабана
    тонеры с наибольшим пересечением списка моделей — там, где код пары не выдал (у HP, Xerox,
    Konica барабан и тонер называются кодами из разных серий, цифрами их не сшить)."""
    drums = [v for v in items.values()
             if v["kind"] == "drum" and (v["platform"], v["account"], v["item_id"]) not in have]
    out = []
    for k, src in enumerate(drums):
        cores = db.query("""SELECT DISTINCT model_core FROM compat_index
                             WHERE platform=%s AND account=%s AND item_id=%s AND verdict='yes'
                             LIMIT 40""",
                         (src["platform"], src["account"], src["item_id"]))
        cores = [c["model_core"] for c in cores]
        if len(cores) < 2:
            continue
        rows = db.query("""SELECT item_id, count(DISTINCT model_core) shared
                             FROM compat_index
                            WHERE platform=%s AND account=%s AND item_kind='toner'
                              AND verdict='yes' AND model_core = ANY(%s)
                            GROUP BY item_id HAVING count(DISTINCT model_core) >= 2
                            ORDER BY shared DESC, item_id LIMIT %s""",
                        (src["platform"], src["account"], cores, MAX_PER_LINK))
        for i, r in enumerate(rows, 1):
            dst = items.get((src["platform"], src["account"], str(r["item_id"])))
            if not dst:
                continue
            out.append(_row(src, dst, "drum_toner", f"models={r['shared']}", i))
            out.append(_row(dst, src, "drum_toner", f"models={r['shared']}", i))
        if verbose and k and k % 1000 == 0:
            print(f"  барабанов обработано {k}/{len(drums)}", flush=True)
    return out


def drum_toner(items, verbose=False):
    rows = _drum_toner_by_code(items)
    have = {(r[0], r[1], r[3]) for r in rows}
    rows += _drum_toner_by_models(items, have, verbose=verbose)
    return rows


def kit_components(items):
    """Комплект ↔ его составные. Комплект — по типу товара и по названию («комплект», «набор»,
    «+», «(N шт.)»); составные — лоты того же канала с кодом расходника из названия комплекта."""
    by_code = _by_code(items)
    out = []
    for src in items.values():
        is_kit = src["kind"] == "kit" or bool(_KIT_RX.search(src["title"] or ""))
        if not is_kit:
            continue
        seen = set()
        for code in codes_in_title(src["title"]):
            cand = [v for v in by_code.get((src["platform"], src["account"], code), [])
                    if v["kind"] != "kit" and not _KIT_RX.search(v["title"] or "")
                    and v["item_id"] != src["item_id"]]
            cand.sort(key=lambda v: (v["title"] or ""))
            for dst in cand[:MAX_PER_LINK]:
                if dst["item_id"] in seen:
                    continue
                seen.add(dst["item_id"])
                out.append(_row(src, dst, "kit_component", f"kit-code={code}", len(seen)))
                out.append(_row(dst, src, "kit_component", f"kit-code={code}", 1))
    return out


# ─────────────────────────── струйные: чёрный ↔ цветной (color_pair) ───────────────────────────
# У струйных расходник делится по цвету, и спрашивают ровно об этом: «а цветной такой есть?»,
# «а чёрный отдельно?». Сторону берём из кода и из названия:
#   Canon — чёрный PG/PGI, цветной CL/CLI, номера идут парой (PG-510 ↔ CL-511, PG-440 ↔ CL-441,
#           PG-445 ↔ CL-446, PGI-450 ↔ CLI-451): номер цветного = номер чёрного + 1;
#   HP    — номер ОДИН на обе стороны (122, 123, 652, 653), сторону решает слово в названии;
#   Epson — пара внутри серии по коду: T0921 (чёрный) ↔ T0922…T0924 (цветные).
# Бренд в ключ входит обязательно: голый номер 664 есть и у HP, и у Epson — без бренда они бы
# слиплись в одну «пару».
_BLACK_RX = re.compile(r"ч[её]рн|\bblack\b", re.I)
_COLOR_RX = re.compile(r"цветн|тр[её]хцветн|многоцветн|\bcolor\b|\bcolour\b", re.I)
_CANON_BK_RX = re.compile(r"^PGI?-?(\d{2,4})[A-Z]*$")
_CANON_CL_RX = re.compile(r"^CLI?-?(\d{2,4})[A-Z]*$")
_EPSON_SER_RX = re.compile(r"^T(\d{3})(\d)$")
_PLAIN_NUM_RX = re.compile(r"^(\d{2,4})[A-Z]{0,2}$")


def _is_single_ink(v):
    """Одиночный струйный лот: комплект «PG-510+CL-511» в паре не участвует — в нём уже оба."""
    return v["kind"] == "ink" and not _KIT_RX.search(v["title"] or "")


def _color_side(v):
    """'black' | 'color' | None — какая сторона пары. Код сильнее названия: у Canon он однозначен."""
    code = (v["code"] or "").upper().replace(" ", "")
    if _CANON_CL_RX.match(code):
        return "color"
    if _CANON_BK_RX.match(code):
        return "black"
    t = v["title"] or ""
    if _COLOR_RX.search(t):
        return "color"
    if _BLACK_RX.search(t):
        return "black"
    m = _EPSON_SER_RX.match(code)
    if m:
        return "black" if m.group(2) == "1" else "color"
    return None


def _color_key(v):
    """Ключ пары: у обеих сторон он обязан совпасть. None — код в пару не складывается."""
    code = (v["code"] or "").upper().replace(" ", "")
    m = _CANON_BK_RX.match(code)
    if m:
        return f"canon:{int(m.group(1))}"
    m = _CANON_CL_RX.match(code)
    if m:
        return f"canon:{int(m.group(1)) - 1}"
    m = _EPSON_SER_RX.match(code)
    if m:
        return f"epson:{m.group(1)}"
    m = _PLAIN_NUM_RX.match(code)
    if m:
        return f"{(v['brand'] or '?').lower()}:{int(m.group(1))}"
    return None


def _color_pairs_by_code(items):
    g = collections.defaultdict(lambda: {"black": [], "color": []})
    for v in items.values():
        if not _is_single_ink(v):
            continue
        side, key = _color_side(v), _color_key(v)
        if side and key:
            g[(v["platform"], v["account"], key)][side].append(v)
    out = []
    for key, sides in g.items():
        for a, b in (("black", "color"), ("color", "black")):
            cand = sorted(sides[b], key=lambda v: (v["title"] or ""))
            for src in sides[a]:
                for i, dst in enumerate(cand[:MAX_PER_LINK], 1):
                    out.append(_row(src, dst, "color_pair",
                                    f"code={src['code']}~{dst['code']}", i))
    return out


def _color_pairs_by_models(items, have, verbose=False):
    """Правило совместимости — для карточек БЕЗ кода в названии («Картридж для Canon Pixma MP492»;
    таких у нас сотни — отдельная карточка на каждую модель принтера). Сосед берётся из того же
    канала по общей серии принтеров и обязан сам знать свою сторону: в блок для ответа он попадёт
    со своим названием («… CL-511 … цветной»), и спросивший «есть цветной?» получит именно его."""
    ink = {k: v for k, v in items.items() if _is_single_ink(v)}
    rows = db.query("""SELECT platform, account, item_id, model_core FROM compat_index
                        WHERE verdict='yes' AND item_kind='ink' AND model_core IS NOT NULL""")
    cores = collections.defaultdict(set)
    by_core = collections.defaultdict(set)
    for r in rows:
        k = (r["platform"], r["account"], str(r["item_id"]))
        if k not in ink:
            continue
        cores[k].add(r["model_core"])
        by_core[(r["platform"], r["account"], r["model_core"])].add(k)
    out = []
    for k, src in ink.items():
        if k in have or len(cores.get(k, ())) < 2:
            continue
        side = _color_side(src)
        shared = collections.Counter()
        for mc in cores[k]:
            for n in by_core[(src["platform"], src["account"], mc)]:
                if n != k:
                    shared[n] += 1
        cand = []
        for n, sh in shared.most_common():
            if sh < 2:
                break
            dst = ink[n]
            dside = _color_side(dst)
            if not dside or (side and dside == side):
                continue
            cand.append((dst, sh))
            if len(cand) >= MAX_PER_LINK:
                break
        for i, (dst, sh) in enumerate(cand, 1):
            out.append(_row(src, dst, "color_pair", f"models={sh};to={_color_side(dst)}", i))
    if verbose:
        print(f"  color_pair по совместимости: {len(out)} строк", flush=True)
    return out


def color_pairs(items, verbose=False):
    rows = _color_pairs_by_code(items)
    have = {(r[0], r[1], r[3]) for r in rows}
    return rows + _color_pairs_by_models(items, have, verbose=verbose)


# ──────────────────────────────────── сборка таблицы ────────────────────────────────────

_COLS = ("platform", "account", "rel_type", "item_from", "item_to", "article_from", "article_to",
         "kind_from", "kind_to", "title_to", "url_to", "ref_to", "basis", "rank")
_INSERT = (f"INSERT INTO sku_relations ({', '.join(_COLS)}) VALUES %s "
           f"ON CONFLICT (platform, account, rel_type, item_from, item_to) DO NOTHING")


def _cap(rows):
    """Не больше MAX_PER_LINK соседей одного типа на лот: у ходовых кодов кандидатов десятки,
    а покупателю нужен один-два. Порядок сохраняем — первым идёт лучший."""
    seen = collections.Counter()
    out = []
    for r in rows:
        key = (r[0], r[1], r[2], r[3])
        if seen[key] >= MAX_PER_LINK:
            continue
        seen[key] += 1
        out.append(r[:13] + (seen[key],))
    return out


def build(verbose=True):
    """Полная пересборка. TRUNCATE и вставка — в одной транзакции: сборка упала на середине →
    откат, в таблице остаётся прежний набор связей, ответы продолжают работать на нём."""
    items = load_items(verbose=verbose)
    rows = []
    for name, fn in (("chip_pair", lambda: chip_pairs(items)),
                     ("drum_toner", lambda: drum_toner(items, verbose=verbose)),
                     ("kit_component", lambda: kit_components(items)),
                     ("color_pair", lambda: color_pairs(items, verbose=verbose))):
        part = fn()
        rows += part
        if verbose:
            print(f"  {name}: {len(part)} строк")
    rows = _cap(rows)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE sku_relations")
            psycopg2.extras.execute_values(cur, _INSERT, rows, page_size=1000)
            cur.execute("SELECT count(*), count(DISTINCT (platform, account, item_from)) "
                        "FROM sku_relations")
            rows_total, items_total = cur.fetchone()
            cur.execute("""INSERT INTO sku_relations_meta (id, built_at, rows_total, items_total)
                           VALUES (1, now(), %s, %s)
                           ON CONFLICT (id) DO UPDATE SET built_at=now(),
                               rows_total=EXCLUDED.rows_total, items_total=EXCLUDED.items_total""",
                        (rows_total, items_total))
    if verbose:
        print(f"Связи собраны: {rows_total} строк по {items_total} лотам", flush=True)
    return rows_total, items_total


def meta():
    rows = db.query("""SELECT built_at, rows_total, items_total,
                              extract(epoch FROM now() - built_at)/3600 AS age_hours
                         FROM sku_relations_meta WHERE id = 1""")
    if not rows:
        return None
    m = dict(rows[0])
    m["age_hours"] = float(m["age_hours"])
    return m


def rebuild_if_stale(verbose=False):
    """Шаг цикла ответов: пересобрать связи, если номенклатура (compat_index) свежее их.

    Гейт по индексу, а не по часам: связи строятся ИЗ индекса, и пока индекс не пересобирали,
    пересобирать связи не из чего. Возвращает (rows, items) при сборке или None."""
    from reports import compat_index
    ci, m = compat_index.meta(), meta()
    if not ci or not ci["rows_total"]:
        return None
    if m and m["rows_total"] and m["built_at"] >= ci["built_at"]:
        return None
    return build(verbose=verbose)


# ──────────────────────────────────── чтение связей ────────────────────────────────────

def for_item(platform, account, item_id, rel_types=None, limit=3):
    """Связи лота в ЕГО канале. account обязателен по смыслу (см. catalog._same_channel), но
    account=None допускаем для внутренних вызовов и старых данных — тогда сужаем по площадке."""
    where = ["platform=%s", "item_from=%s"]
    params = [platform, str(item_id)]
    if account:
        where.append("account=%s")
        params.append(account)
    if rel_types:
        where.append("rel_type = ANY(%s)")
        params.append(list(rel_types))
    return db.query(f"""SELECT rel_type, item_to, article_to, kind_to, title_to, url_to, ref_to, basis
                          FROM sku_relations WHERE {' AND '.join(where)}
                         ORDER BY rank, rel_type LIMIT %s""", tuple(params) + (limit,))


# Что спросили — то и подставляем. Вопрос про чип на бесчиповом лоте → chip_pair; «что в комплекте»
# на барабане/тонере → комплект и парный расходник; «а тонер / а барабан отдельно?» → drum_toner.
_ASK_CHIP = re.compile(r"чип", re.I)
_ASK_KIT = re.compile(r"в\s+комплект|комплектац|что\s+вход|идёт\s+ли|идет\s+ли|набор", re.I)
_ASK_DRUM = re.compile(r"фотобарабан|барабан|\bdrum\b|драм", re.I)
_ASK_TONER = re.compile(r"тонер|тубу|туба|порошок", re.I)
# «есть цветной?» / «а чёрный?» — вопрос про соседний лот другого цвета (только у струйных)
_ASK_COLOR = re.compile(r"цветн|тр[её]хцветн|многоцветн|\bcolor\b|ч[её]рн\w*\s|ч[её]рный|ч[её]рного|"
                        r"\bbk\b|\bblack\b", re.I)


def types_for_question(text, card_chip=None, card_kind=None):
    """Какие связи уместны в этом вопросе. Пустой список → в промпт ничего не добавляем."""
    q = text or ""
    out = []
    # состояние чипа отдельно проверять не нужно: chip_pair существует ТОЛЬКО у бесчиповых лотов
    # (см. chip_pairs) — есть строка, значит лот бесчиповый. card_chip оставлен как явный запрет:
    # если карточка говорит «чип установлен», предлагать «версию с чипом» бессмысленно.
    if _ASK_CHIP.search(q) and card_chip in (None, "none"):
        out.append("chip_pair")
    if _ASK_KIT.search(q):
        out += ["kit_component", "drum_toner"]
    if (_ASK_DRUM.search(q) and card_kind != "drum") or (_ASK_TONER.search(q) and card_kind == "drum"):
        out.append("drum_toner")
    if _ASK_COLOR.search(q):
        out.append("color_pair")
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


_RU = {"chip_pair": "версия того же картриджа С ЧИПОМ",
       "drum_toner": "парный расходник (барабан ↔ тонер) под ту же серию",
       "kit_component": "комплект / его составная часть",
       "color_pair": "парный струйный картридж другого цвета (чёрный ↔ цветной)"}


def relations_block(platform, account, item_id, question, card_chip=None, card_kind=None):
    """Блок СВЯЗАННЫЕ ЛОТЫ для промпта или '' — если вопрос не про соседний лот либо связи нет."""
    types = types_for_question(question, card_chip, card_kind)
    if not types:
        return ""
    rows = for_item(platform, account, item_id, types, limit=4)
    if not rows:
        return ""
    lines = ["СВЯЗАННЫЕ ЛОТЫ — наша же номенклатура того же магазина (можно назвать артикул и ссылку; "
             "если нужного варианта здесь нет — не выдумывать, наличие не утверждать):"]
    for r in rows:
        lines.append(f"- {_RU.get(r['rel_type'], r['rel_type'])}: {(r['title_to'] or '')[:80]} — "
                     f"{r['ref_to']}, ссылка {r['url_to']}")
    return "\n".join(lines)


CHIP_LINE = "Если оригинала нет — есть версия с чипом: {url}"

# Строка про версию с чипом идёт на ЛЮБОЙ вопрос о товаре (правило Сергея 10.09.2026): человек,
# спросивший «подойдёт ли к M211?» или «какой ресурс?», про чип обычно не знает — а именно чип
# и решает, заработает ли у него бесчиповый картридж. Не добавляем только там, где спрашивают не
# о товаре, а о сделке: наличие, доставка, цена. Прямой вопрос про чип этот запрет перебивает.
_ASK_DEAL = re.compile(
    r"нали[чт]|на\s+складе|остал[ои]сь|когда\s+(?:будет|придёт|придет|привез|отправ|достав)|"
    r"доставк|достав(?:ите|ят|ка)|срок\w*\s+(?:достав|отправ)|отправ(?:ка|ите|ляете)|"
    r"цен[аыу]|стоимост|сколько\s+стоит|подешевле|дешевле|скидк|торг|чек\b|"
    r"самовывоз|пвз|курьер", re.I)


def chip_line(platform, account, item_id, question, card_chip=None):
    """Готовая строка в ответ про чип на бесчиповом лоте. Пары нет — '' (ничего не добавляем)."""
    q = question or ""
    if card_chip == "installed" or not q.strip():
        return ""
    if _ASK_DEAL.search(q) and not _ASK_CHIP.search(q):
        return ""
    rows = for_item(platform, account, item_id, ["chip_pair"], limit=1)
    return CHIP_LINE.format(url=rows[0]["url_to"]) if rows and rows[0]["url_to"] else ""


def with_chip_line(reply, platform, account, item_id, question, card_chip=None):
    """Дописать строку про версию с чипом к готовому ответу (один раз, без дублей)."""
    line = chip_line(platform, account, item_id, question, card_chip)
    if not line or not (reply or "").strip():
        return reply
    if rows_url(line) and rows_url(line) in (reply or ""):
        return reply
    return reply.rstrip() + "\n" + line


def rows_url(line):
    m = re.search(r"https?://\S+", line or "")
    return m.group(0) if m else None


# ──────────────────────────────────────── CLI ────────────────────────────────────────

def stats():
    print("Связи номенклатуры sku_relations")
    m = meta()
    print(f"  собрано: {m['built_at']:%Y-%m-%d %H:%M} ({m['age_hours']:.1f} ч назад)" if m
          else "  НЕ СОБРАНЫ")
    for r in db.query("""SELECT rel_type, account, count(*) n,
                                count(DISTINCT item_from) items
                           FROM sku_relations GROUP BY 1,2 ORDER BY 1,2"""):
        print(f"  {r['rel_type']:<14} {r['account']:<9} строк {r['n']:>6}  лотов {r['items']:>6}")
    for r in db.query("SELECT rel_type, count(*) n FROM sku_relations GROUP BY 1 ORDER BY 1"):
        print(f"  ИТОГО {r['rel_type']:<14} {r['n']}")


def check(article):
    rows = db.query("""SELECT platform, account, rel_type, item_from, article_from, item_to,
                              article_to, left(title_to,60) title_to, ref_to, basis
                         FROM sku_relations
                        WHERE article_from = %s OR article_from LIKE %s
                        ORDER BY rel_type, platform, account, rank LIMIT 40""",
                    (article, article + "%"))
    print(f"Связи по артикулу {article}: {len(rows)}")
    for r in rows:
        print(f"  {r['rel_type']:<14} {r['platform']}/{r['account']} {r['article_from']} "
              f"[{r['item_from']}] → {r['article_to']} [{r['item_to']}] {r['basis']} | {r['title_to']}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--stats"
    if arg == "--build":
        build()
    elif arg == "--check":
        check(sys.argv[2])
    else:
        stats()
