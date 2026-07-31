#!/usr/bin/env python3
# поток: gab
"""Перепроверка мэтчинга Solutions Print ПО НАЗВАНИЯМ товаров (независимо от артикула).

Зачем: артикул SP — внутренний числовой код cartridge.ru (83730, 245288), по нему OEM не
проверяется, из-за чего SP считался «непроверяемым» источником. Но в НАЗВАНИИ строки прайса
модель картриджа есть почти всегда («Картридж Samsung CLT-K508L», «Картридж HP 90X (CE390X)»).
Скрипт проверяет привязку по названию и сверяет короб SP с тем, что реально записан в карточку.

Read-only. Никаких записей в маркетплейсы. Пишет только CSV/MD в docs/.

Запуск:  ./venv/bin/python tools/gab_sp_title_verify.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/opt/mp-analytics")

import openpyxl  # noqa: E402

from core import db  # noqa: E402

OUT_CSV = "docs/solutionsprint_title_check.csv"
OUT_MD = "docs/reports/12_solutionsprint_title_match_2026-07-29.md"

_NORM = re.compile(r"[^A-Za-zА-Яа-яЁё0-9]")
_LAT = re.compile(r"[A-Za-z0-9]")
_CYR_ONLY = re.compile(r"^[А-Яа-яЁё()\-]+$")
_RANGE = re.compile(r"^([A-Za-z]*\d+[A-Za-z]*)[-–]([A-Za-z]*\d+[A-Za-z]*)$")


def norm(s: str) -> str:
    return _NORM.sub("", (s or "")).upper()


def card_models(title: str) -> list[str]:
    """Модель(и) из НАШЕГО названия: между носителем и словом «для».

    «Картридж CF283A для HP LJ Pro M125 черный» → ['CF283A']
    «Картриджи Q6470A-Q6473A для HP LJ 3600»    → ['Q6470A','Q6473A','Q6470A-Q6473A']
    """
    t = (title or "").strip()
    head = re.split(r"\s+для\s+", t, maxsplit=1)[0]
    toks = head.split()
    # снять ведущие русские слова-носители (Картридж, Фотобарабан, Заправочный картридж, …)
    while toks and _CYR_ONLY.match(toks[0]):
        toks.pop(0)
    if not toks:
        return []
    out = []
    for tok in toks[:2]:
        tok = tok.strip(",;")
        if not _LAT.search(tok):
            break
        out.append(tok)
        m = _RANGE.match(tok)
        if m:
            out.extend([m.group(1), m.group(2)])
    return [x for x in dict.fromkeys(out) if len(norm(x)) >= 3]


_RUN = re.compile(r"[A-Za-z]+|\d+")
_MODEL_RX_CACHE: dict[str, re.Pattern] = {}


def model_rx(model: str) -> re.Pattern | None:
    """Регэксп поиска кода модели в ИСХОДНОМ тексте.

    Разделители внутри кода необязательны (TN-2420 ≡ TN2420 ≡ TN 2420), а границы
    строгие: слева и справа не должно быть латинской буквы/цифры. Так «TK-60» не
    подцепится внутри «TK-6017», но подцепится в «TN-2420/2425».
    """
    if model in _MODEL_RX_CACHE:
        return _MODEL_RX_CACHE[model]
    runs = _RUN.findall(model)
    if not runs or len("".join(runs)) < 3:
        _MODEL_RX_CACHE[model] = None
        return None
    pat = r"[\s\-/._]*".join(re.escape(x) for x in runs)
    rx = re.compile(r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])", re.I)
    _MODEL_RX_CACHE[model] = rx
    return rx


def contains_model(title: str, model: str) -> bool:
    rx = model_rx(model)
    return bool(rx and rx.search(title or ""))


def fnum(x):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return None


def sides(l, w, h):
    v = [fnum(l), fnum(w), fnum(h)]
    return sorted(v) if all(x is not None for x in v) else None


def agree(a, b) -> bool:
    """Правило п.9: согласие = по каждой стороне ≤10% ИЛИ ≤1.5 см (сортированные ДхШхВ)."""
    if not a or not b:
        return False
    for x, y in zip(a, b):
        if abs(x - y) <= 1.5:
            continue
        if max(x, y) > 0 and abs(x - y) / max(x, y) <= 0.10:
            continue
        return False
    return True


def main():
    sp = db.query(
        """select article, title, length_cm, width_cm, height_cm, weight_kg, volume_l
           from supplier_dims where supplier='solutionsprint'
             and length_cm is not null and width_cm is not null and height_cm is not null"""
    )
    sp = [dict(r) for r in sp]
    for r in sp:
        r["_norm"] = norm(r["title"])
        r["_sides"] = sides(r["length_cm"], r["width_cm"], r["height_cm"])
    print(f"строк прайса Solutions Print с коробом: {len(sp)}")

    # индекс: первые 4 нормализованных символа названия → строки (сужение перебора)
    idx = defaultdict(list)
    for r in sp:
        for k in range(len(r["_norm"]) - 3):
            idx[r["_norm"][k:k + 4]].append(r)

    wb = openpyxl.load_workbook("docs/coverage.xlsx", read_only=True)
    ws = wb["Покрыто"]
    it = ws.iter_rows(values_only=True)
    h = next(it)
    hi = {x: i for i, x in enumerate(h)}
    cards = []
    for r in it:
        if r[hi["vendorCode"]] is None:
            continue
        src = str(r[hi["источник"]] or "")
        if not src.startswith("solutionsprint"):
            continue
        if str(r[hi["способ"]]) != "прямой":
            continue
        cards.append(
            {
                "vendorCode": str(r[hi["vendorCode"]]),
                "nmID": r[hi["nmID"]],
                "title": r[hi["title"]],
                "L": r[hi["L"]], "W": r[hi["W"]], "H": r[hi["H"]],
                "источник": src,
            }
        )
    # дедуп по vendorCode (карточка есть в обоих кабинетах)
    seen = set()
    uniq = []
    for c in cards:
        if c["vendorCode"] in seen:
            continue
        seen.add(c["vendorCode"])
        uniq.append(c)
    cards = uniq
    print(f"карточек-матерей с источником solutionsprint (прямой, уник. артикул): {len(cards)}")

    res = []
    cnt = Counter()
    for c in cards:
        models = card_models(c["title"])
        rec = sides(c["L"], c["W"], c["H"])
        hit_rows = []
        for m in models:
            rx = model_rx(m)
            if not rx:
                continue
            for r in sp:
                if rx.search(r["title"] or ""):
                    hit_rows.append((m, r))
        # дедуп строк прайса
        ded = {}
        for m, r in hit_rows:
            ded.setdefault(r["article"], (m, r))
        hit_rows = list(ded.values())

        def volume(r):
            s = r["_sides"]
            return (s[0] * s[1] * s[2] / 1000.0) if s else None

        recv = (rec[0] * rec[1] * rec[2] / 1000.0) if rec else None
        spread_rows = []
        if not models:
            verdict = "модель_не_извлечена"
        elif not hit_rows:
            verdict = "в_прайсе_по_названию_не_найдено"
        else:
            ok = [(m, r) for m, r in hit_rows if agree(rec, r["_sides"])]
            if not ok:
                verdict = "расходится"
            else:
                # есть согласная строка, но нет ли среди совпавших по названию
                # строк радикально другого короба (≥1.5× по объёму)?
                for m, r in hit_rows:
                    v = volume(r)
                    if v and recv and (v / recv >= 1.5 or recv / v >= 1.5):
                        spread_rows.append((m, r))
                verdict = "подтверждено_с_разбросом" if spread_rows else "подтверждено"
        cnt[verdict] += 1

        best = None
        if hit_rows:
            if verdict.startswith("подтверждено"):
                best = next((m, r) for m, r in hit_rows if agree(rec, r["_sides"]))
            else:
                # для расхождения показываем строку с максимальным объёмом (самая опасная разница)
                best = max(hit_rows, key=lambda mr: (mr[1]["_sides"] or [0, 0, 0])[2])
        res.append(
            {
                "vendorCode": c["vendorCode"],
                "nmID": c["nmID"],
                "title": c["title"],
                "модель_из_названия": "/".join(models),
                "записано_ДхШхВ": f"{c['L']}x{c['W']}x{c['H']}",
                "источник": c["источник"],
                "вердикт": verdict,
                "SP_артикул": best[1]["article"] if best else "",
                "SP_название": best[1]["title"] if best else "",
                "SP_ДхШхВ": (f"{best[1]['length_cm']}x{best[1]['width_cm']}x{best[1]['height_cm']}"
                             if best else ""),
                "совпало_по": best[0] if best else "",
                "строк_SP_по_названию": len(hit_rows),
                "разброс_строк": len(spread_rows),
                "разброс_пример": (f"{spread_rows[0][1]['article']} "
                                   f"{spread_rows[0][1]['length_cm']}x{spread_rows[0][1]['width_cm']}"
                                   f"x{spread_rows[0][1]['height_cm']} «{str(spread_rows[0][1]['title'])[:60]}»")
                                  if spread_rows else "",
            }
        )

    total = len(cards)
    print("\n=== ИТОГ ПРОВЕРКИ ПО НАЗВАНИЯМ")
    for k, v in cnt.most_common():
        print(f"  {k:34s} {v:5d}  ({100*v/total:.1f}%)")

    with io.open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(res[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(res)
    print(f"\nCSV: {OUT_CSV} ({len(res)} строк)")

    print("\n--- 6 примеров ПОДТВЕРЖДЕНО")
    for r in [x for x in res if x["вердикт"] == "подтверждено"][:6]:
        print(f"  vc={r['vendorCode']} {str(r['title'])[:62]}")
        print(f"     модель={r['модель_из_названия']} → SP art={r['SP_артикул']} «{str(r['SP_название'])[:62]}»")
        print(f"     SP {r['SP_ДхШхВ']} ≈ записано {r['записано_ДхШхВ']}")
    print("\n--- 8 примеров ПОДТВЕРЖДЕНО, НО ЕСТЬ РАЗБРОС строк прайса")
    for r in [x for x in res if x["вердикт"] == "подтверждено_с_разбросом"][:8]:
        print(f"  vc={r['vendorCode']} {str(r['title'])[:62]}")
        print(f"     записано {r['записано_ДхШхВ']} (согласная строка SP {r['SP_артикул']} {r['SP_ДхШхВ']})")
        print(f"     но по названию также подходит: {r['разброс_пример']}")
    print("\n--- 6 примеров РАСХОДИТСЯ")
    for r in [x for x in res if x["вердикт"] == "расходится"][:6]:
        print(f"  vc={r['vendorCode']} {str(r['title'])[:62]}")
        print(f"     модель={r['модель_из_названия']} → SP art={r['SP_артикул']} «{str(r['SP_название'])[:62]}»")
        print(f"     SP {r['SP_ДхШхВ']}  ≠  записано {r['записано_ДхШхВ']}")
    print("\n--- 6 примеров НЕ НАЙДЕНО/НЕ ИЗВЛЕЧЕНО")
    for r in [x for x in res if x["вердикт"] in ("модель_не_извлечена","в_прайсе_по_названию_не_найдено")][:6]:
        print(f"  vc={r['vendorCode']} [{r['вердикт']}] модель={r['модель_из_названия']!r} :: {str(r['title'])[:70]}")


if __name__ == "__main__":
    main()
