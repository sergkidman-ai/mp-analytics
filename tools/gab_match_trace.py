#!/usr/bin/env python3
# поток: gab
"""Трассировка мэтчинга «строка прайса поставщика → наша карточка → взятый размер».

Read-only: читает БД (supplier_dims, wb_cards) и docs/coverage.xlsx, ничего не пишет
в маркетплейсы. Результат — CSV/MD с ПОЛНОЙ цепочкой доказательства по каждому примеру.

Идея верификации (а не пересказа): для карточки из листа «Покрыто» с источником <поставщик>
мы ищем в прайсе этого поставщика строку, у которой (а) ДхШхВ совпадает с тем, что реально
записано в coverage.xlsx, и (б) извлечённый код модели пересекается с кодом нашей карточки.
Такая строка и есть фактический источник размера — цепочка подтверждена числами, а не памятью.

Запуск:
    ./venv/bin/python tools/gab_match_trace.py --examples 5
    ./venv/bin/python tools/gab_match_trace.py --supplier sakura --examples 20
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/mp-analytics")

import openpyxl  # noqa: E402

from core import db  # noqa: E402

CLEAN_SUPPLIERS = ("sakura", "cactus", "изи", "rapid", "solutionsprint")

# --- нормализация -----------------------------------------------------------
_NORM_RX = re.compile(r"[^A-Z0-9]")


def norm(s: str) -> str:
    return _NORM_RX.sub("", (s or "").upper())


# Префиксы поставщицких артикулов, которые надо снять, чтобы получить OEM-код.
# Порядок важен: длинные раньше коротких.
SUP_PREFIX = {
    "sakura": ("SA", "SAT", "S"),
    "cactus": ("CS-", "CS", "GG-", "GG", "PR-", "PR"),
    "изи": ("IC-", "DC-", "TC-", "PC-", "IC", "DC", "TC", "PC"),
    "rapid": ("SF-", "SF", "LC-", "LC", "IC-", "DR-", "TN-"),
    "solutionsprint": (),
}

# Семейства картриджных кодов. Ровно те формы, что реально встречаются в прайсах
# и наших названиях. Модели ПРИНТЕРОВ намеренно не ловим (см. правило B: дети не
# матчатся по прайсу самостоятельно).
FAM = [
    r"\d{3}R\d{4,5}",                        # Xerox 106R01481 / 006R01517
    r"MLT-?D\d{3}[A-Z]{0,2}",                # Samsung MLT-D111L
    r"CLT-?[KCMY]?\d{3}[A-Z]{0,2}",          # Samsung CLT-K508L
    r"C-?EXV-?\d{1,3}[A-Z]{0,2}",            # Canon C-EXV49
    r"[CG]PR-?\d{1,3}",                      # Canon GPR-53
    r"CRG-?\d{2,3}[A-Z]{0,2}",               # Canon CRG-052
    r"TN-?\d{2,4}[A-Z]{0,3}",                # Brother TN-2420
    r"DR-?\d{2,4}[A-Z]{0,3}",                # Brother DR-6000
    r"TK-?\d{3,4}[A-Z]{0,2}",                # Kyocera TK-3170
    r"DK-?\d{3,4}",                          # Kyocera DK-580
    r"C[EFB]\d{3}[AXYUD]?",                  # HP CF289X / CE413A / CB540A
    r"W[12]\d{3}[AXY]?",                     # HP W2033X / W1106A
    r"C[CN]\d{3}[A-Z]?",                     # HP CC364A
    r"Q\d{4}[AX]?",                          # HP Q2612A
    r"C13[A-Z]\d{2}[A-Z0-9]\d{2}[A-Z]?",     # Epson C13T03V14A
    r"T\d{2,4}[A-Z]{0,2}",                   # Epson T06BK / T1281
    r"KX-?FAD?\d{2,3}[A-Z]?",                # Panasonic KX-FAD89A / KX-FAT88A
    r"KX-?FAT\d{2,3}[A-Z]?",
    r"MP-?C?\d{3,4}[A-Z]{0,2}",              # Ricoh MP2501
    r"SP-?\d{3,4}[A-Z]{0,2}",                # Ricoh SP311
    r"AR-?\d{3}[A-Z]{0,2}",                  # Sharp AR-202
    r"ML-?D?\d{3,4}[A-Z]{0,2}",              # Samsung ML-1610
    r"SCX-?D?\d{3,4}[A-Z]{0,2}",             # Samsung SCX-4200
    r"CE\d{3}[AX]",                          # HP CE285A
    r"\d{2,3}[AX]\b",                        # HP 12A / 85A / 106A (короткий код)
]
RX = re.compile(r"(?<![A-Z0-9])(?:%s)(?![A-Z0-9])" % "|".join(FAM), re.I)

# Короткие коды (12A, 85A) сами по себе слишком общие — считаем их валидными только
# если рядом в тексте есть вендор или полный код. Помечаем отдельно.
SHORT_RX = re.compile(r"^\d{2,3}[AX]$")


def codes(text: str) -> set[str]:
    """Нормализованные коды моделей из произвольного текста."""
    if not text:
        return set()
    out = set()
    for m in RX.findall(text.upper()):
        n = norm(m)
        if len(n) >= 3:
            out.add(n)
    return out


def strip_prefix(supplier: str, article: str) -> str:
    """Снять поставщицкий префикс с артикула → кандидат OEM-кода."""
    a = (article or "").strip().upper()
    for p in sorted(SUP_PREFIX.get(supplier, ()), key=len, reverse=True):
        if a.startswith(p):
            return a[len(p):]
    return a


def supplier_codes(supplier: str, article: str, title: str) -> tuple[set[str], set[str]]:
    """(коды из артикула, коды из названия) для строки прайса."""
    from_art = codes(article) | codes(strip_prefix(supplier, article))
    # голый артикул без префикса тоже кандидат (напр. изи IC-H21XL → H21XL)
    bare = norm(strip_prefix(supplier, article))
    if bare and len(bare) >= 4 and not bare.isdigit():
        from_art.add(bare)
    return from_art, codes(title)


def fnum(x):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None


def dims_key(l, w, h):
    v = [fnum(l), fnum(w), fnum(h)]
    if any(x is None for x in v):
        return None
    return tuple(round(x, 1) for x in v)


# --- загрузка данных --------------------------------------------------------
def load_supplier_index():
    """{supplier: {dims_key: [row,…]}} + {supplier: [row,…]}"""
    rows = db.query(
        """select supplier, article, title, length_cm, width_cm, height_cm,
                  weight_kg, volume_l, barcode, src_file
           from supplier_dims
           where supplier = ANY(%s)
             and length_cm is not null and width_cm is not null and height_cm is not null""",
        (list(CLEAN_SUPPLIERS),),
    )
    by_dims = defaultdict(lambda: defaultdict(list))
    by_sup = defaultdict(list)
    for r in rows:
        d = dict(r)
        d["codes_art"], d["codes_title"] = supplier_codes(
            d["supplier"], d["article"], d["title"]
        )
        k = dims_key(d["length_cm"], d["width_cm"], d["height_cm"])
        d["_dims_key"] = k
        by_sup[d["supplier"]].append(d)
        if k:
            by_dims[d["supplier"]][k].append(d)
    return by_dims, by_sup


def load_covered(path="docs/coverage.xlsx"):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["Покрыто"]
    it = ws.iter_rows(values_only=True)
    h = next(it)
    hi = {x: i for i, x in enumerate(h)}
    out = []
    for r in it:
        if r[hi["vendorCode"]] is None:
            continue
        out.append(
            {
                "vendorCode": str(r[hi["vendorCode"]]),
                "nmID": r[hi["nmID"]],
                "группа": r[hi["группа"]],
                "title": r[hi["title"]],
                "L": r[hi["L"]],
                "W": r[hi["W"]],
                "H": r[hi["H"]],
                "источник": r[hi["источник"]] or "",
                "способ": r[hi["способ"]] or "",
            }
        )
    return out


def base_supplier(src: str) -> str:
    return re.sub(r"\(.*", "", src or "").strip()


def confirm(row, card_title, card_codes):
    """Чем подтверждена привязка строки прайса к карточке. '' = ничем."""
    inter = (row["codes_art"] | row["codes_title"]) & card_codes
    if inter:
        return "код", sorted(inter)
    # фолбэк: голый код поставщика встречается в названии карточки как подстрока
    t = norm(card_title)
    for c in sorted(row["codes_art"] | row["codes_title"], key=len, reverse=True):
        if len(c) >= 5 and c in t:
            return "вхождение", [c]
    bare = norm(strip_prefix(row["supplier"], row["article"]))
    if len(bare) >= 5 and bare in t:
        return "вхождение", [bare]
    return "", []


def trace(card, by_dims):
    """Найти строку(и) прайса, чьи ДхШхВ реально стоят в карточке."""
    sup = base_supplier(card["источник"])
    if sup not in CLEAN_SUPPLIERS:
        return sup, [], set()
    k = dims_key(card["L"], card["W"], card["H"])
    cand = by_dims[sup].get(k, []) if k else []
    ccodes = codes(card["title"])
    hits = [c for c in cand if confirm(c, card["title"], ccodes)[0]]
    return sup, (hits or cand), ccodes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=int, default=5)
    ap.add_argument("--supplier", default=None)
    ap.add_argument("--out", default=None, help="куда писать markdown")
    args = ap.parse_args()

    by_dims, by_sup = load_supplier_index()
    covered = load_covered()
    mothers = [c for c in covered if c["способ"] == "прямой"]

    stats = defaultdict(lambda: {"total": 0, "code_ok": 0, "sub_ok": 0,
                                 "dims_only": 0, "no_row": 0})
    examples = defaultdict(list)
    seen_vc = defaultdict(set)

    for c in mothers:
        sup = base_supplier(c["источник"])
        if args.supplier and sup != args.supplier:
            continue
        if sup not in CLEAN_SUPPLIERS:
            continue
        s = stats[sup]
        s["total"] += 1
        _, hits, ccodes = trace(c, by_dims)
        k = dims_key(c["L"], c["W"], c["H"])
        exact = by_dims[sup].get(k, []) if k else []
        row, kind, inter = None, "строка не найдена", []
        by_code = [(h, confirm(h, c["title"], ccodes)) for h in hits]
        strong = [(h, cf) for h, cf in by_code if cf[0] == "код"]
        weak = [(h, cf) for h, cf in by_code if cf[0] == "вхождение"]
        if strong:
            s["code_ok"] += 1
            row, (_, inter) = strong[0]
            kind = "код+размер"
        elif weak:
            s["sub_ok"] += 1
            row, (_, inter) = weak[0]
            kind = "вхождение кода+размер"
        elif exact:
            s["dims_only"] += 1
            row, kind = exact[0], "только размер"
        else:
            s["no_row"] += 1
        if row and c["vendorCode"] not in seen_vc[sup] and len(examples[sup]) < args.examples * 6:
            seen_vc[sup].add(c["vendorCode"])
            examples[sup].append({"card": c, "row": row, "kind": kind,
                                  "card_codes": ccodes, "inter": inter})

    out = io.StringIO()
    for sup in CLEAN_SUPPLIERS:
        if args.supplier and sup != args.supplier:
            continue
        s = stats[sup]
        if not s["total"]:
            continue
        ok = s["code_ok"] + s["sub_ok"]
        print(f"\n=== {sup}: матерей(прямой) {s['total']} | цепочка подтверждена {ok} "
              f"({100*ok/s['total']:.1f}%) = код {s['code_ok']} + вхождение {s['sub_ok']} "
              f"| только размер {s['dims_only']} | строка не найдена {s['no_row']}", file=out)
        for ex in examples[sup][: args.examples]:
            c, r = ex["card"], ex["row"]
            print(f"\n  --- {ex['kind']}", file=out)
            print(f"  ПРАЙС  [{sup}] артикул={r['article']!r}", file=out)
            print(f"         название={str(r['title'])[:100]!r}", file=out)
            print(f"         короб={r['length_cm']}x{r['width_cm']}x{r['height_cm']} см"
                  f" | вес={r['weight_kg']} | объём={r['volume_l']} л | файл={r['src_file']}", file=out)
            print(f"  КОД    из артикула={sorted(r['codes_art'])[:6]} из названия={sorted(r['codes_title'])[:6]}", file=out)
            print(f"  КАРТА  vc={c['vendorCode']} nmID={c['nmID']} {str(c['title'])[:80]!r}", file=out)
            print(f"         коды карточки={sorted(ex['card_codes'])[:6]} → ПЕРЕСЕЧЕНИЕ={ex['inter']}", file=out)
            print(f"  РАЗМЕР записан {c['L']}x{c['W']}x{c['H']} см (источник {c['источник']})", file=out)

    text = out.getvalue()
    print(text)
    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
