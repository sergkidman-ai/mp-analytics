#!/usr/bin/env python3
# поток: gab
"""Сколько непокрытых карточек ВБ закрывает эталонная база калибровки (1141 модель).

Эталон — docs/web_search_v2/calibration/calibration_truth.csv: консенсус ≥2 чистых поставщиков,
замеры Solutions Print, паспорт Icecat. Ни одного платного запроса, ни одной интернет-карточки.

Висяки берём из docs/coverage.xlsx: лист «Не покрыто» + «Материнские_без_размера».
Ключ — полный OEM-код из названия. Учитываем наследование: покрыв 4-значного родителя,
закрываем и его детей (артикул начинается с тех же 4 цифр).

Read-only. Пишет только сводку в чат и CSV со списком закрываемых карточек.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/mp-analytics")

import openpyxl

from core import db

ROOT = "/opt/mp-analytics/.claude/worktrees/gab"
TRUTH = f"{ROOT}/docs/web_search_v2/calibration/calibration_truth.csv"
COV = "/opt/mp-analytics/docs/coverage.xlsx"
OUT = "/opt/mp-analytics/docs/truth_coverage_candidates.csv"
SALES_FROM, SALES_TO = "2025-08-01", "2026-07-31"

FAM = [
    r"\d{3}R\d{4,5}", r"MLT-?D\d{3}[A-Z]?", r"CLT-?[KCMY]\d{3}[A-Z]?", r"C-?EXV\d{1,3}",
    r"[CG]PR-?\d{1,3}", r"CRG-?\d{2,3}[A-Z]{0,2}", r"TN-?\d{3,4}[A-Z]{0,2}",
    r"DR-?\d{3,4}[A-Z]{0,2}", r"TK-?\d{3,4}[A-Z]?", r"DK-?\d{3,4}", r"C[EFB]\d{3}[AXYUD]?",
    r"W[12]\d{3}[AX]?", r"C[CN]\d{3}[A-Z]?", r"Q\d{4}[AX]?",
    r"C13[A-Z]\d{2}[A-Z0-9]\d{2}[A-Z]?", r"106R\d{5}|108R\d{5}|013R\d{5}|101R\d{5}",
]
RX = re.compile(r"(?<![A-Z0-9])(?:%s)(?![A-Z0-9])" % "|".join(FAM), re.I)
# Заправка/порошок: код в названии — это код КАРТРИДЖА, под который заправка. Его короб на
# бутылку переносить нельзя (правило потока, см. gab_waves.REFILL_RX).
REFILL_RX = re.compile(r"^\s*(чернила|тонер(?![\s-]*картридж)|заправочн|заправка|тонер-порошок)", re.I)
SET_RX = re.compile(r"комплект|набор", re.I)


def nrm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def one_oem(text):
    c = {nrm(m) for m in RX.findall(str(text or "").upper()) if len(nrm(m)) >= 5}
    return c.pop() if len(c) == 1 else None


def main():
    truth = {}
    with open(TRUTH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            truth[r["oem"]] = r
    print(f"эталон: {len(truth)} моделей")

    sales = defaultdict(lambda: {"qty": 0.0, "rev": 0.0})
    for r in db.query("select article, sum(qty) qty, sum(revenue_buyer) rev from sales "
                      "where platform='wb' and period_from >= %s and period_to <= %s "
                      "group by article", (SALES_FROM, SALES_TO)):
        d = sales[str(r["article"])]
        d["qty"] += float(r["qty"] or 0)
        d["rev"] += float(r["rev"] or 0)

    cards = {str(r["nm_id"]): r for r in
             db.query("select nm_id, vendor_code, title, account from wb_cards")}
    by_prefix = defaultdict(list)
    for c in cards.values():
        vc = str(c["vendor_code"] or "")
        m = re.match(r"^(\d{4})", vc)
        if m and len(vc) > 4:
            by_prefix[m.group(1)].append(c)

    wb = openpyxl.load_workbook(COV, read_only=True, data_only=True)
    holes = {}
    for name, cols in (("Не покрыто", ("vendorCode", "nmID", "title")),
                       ("Материнские_без_размера", ("vendorCode", None, "title"))):
        ws = wb[name]
        it = ws.iter_rows(values_only=True)
        hi = {h: i for i, h in enumerate(next(it))}
        for r in it:
            vc = str(r[hi[cols[0]]] or "")
            nm = str(r[hi[cols[1]]] or "") if cols[1] else ""
            title = r[hi[cols[2]]] or ""
            if vc:
                holes.setdefault(vc, {"vc": vc, "nm": nm, "title": title, "лист": name})
    print(f"висяков ВБ (Не покрыто + Материнские_без_размера): {len(holes)}")

    direct, inherited, skipped = {}, {}, defaultdict(int)
    for vc, h in holes.items():
        t = str(h["title"])
        if REFILL_RX.search(t):
            skipped["заправка/порошок — короб картриджа не переносим"] += 1
            continue
        if SET_RX.search(t):
            skipped["набор/комплект — считается по составу, не по коду"] += 1
            continue
        oem = one_oem(t)
        if not oem:
            skipped["в названии нет однозначного OEM-кода"] += 1
            continue
        if oem not in truth:
            skipped["код есть, но модели нет в эталонной базе"] += 1
            continue
        direct[vc] = {**h, "oem": oem, **truth[oem], "как": "прямое совпадение кода"}
        if len(vc) == 4:
            for ch in by_prefix.get(vc, []):
                cvc = str(ch["vendor_code"])
                if cvc in holes and cvc not in direct:
                    inherited[cvc] = {**holes[cvc], "oem": oem, **truth[oem],
                                      "как": f"наследование от родителя {vc}"}

    covered = {**inherited, **direct}
    def money(d):
        q = r_ = 0.0
        for v in d.values():
            s = sales.get(v["nm"], {"qty": 0, "rev": 0})
            q += s["qty"]; r_ += s["rev"]
        return q, r_

    qd, rd = money(direct)
    qi, ri = money(inherited)
    src = defaultdict(int)
    for v in covered.values():
        src[v["источник"]] += 1

    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["артикул", "nmID", "название", "oem", "длина_см", "ширина_см", "высота_см",
                    "объём_л", "источник_эталона", "подробность", "как_закрыт", "лист",
                    "продажи_12м_шт", "продажи_12м_₽"])
        for v in sorted(covered.values(), key=lambda x: -sales.get(x["nm"], {"rev": 0})["rev"]):
            s = sales.get(v["nm"], {"qty": 0, "rev": 0})
            w.writerow([v["vc"], v["nm"], str(v["title"])[:90], v["oem"], v["длина_см"],
                        v["ширина_см"], v["высота_см"], v["объём_л"], v["источник"],
                        str(v["подробность"])[:90], v["как"], v["лист"],
                        round(s["qty"]), round(s["rev"])])

    print(f"\nЗАКРЫВАЕТСЯ БЕЗ ЕДИНОГО ПЛАТНОГО ЗАПРОСА: {len(covered)} карточек")
    print(f"  прямое совпадение кода : {len(direct):>5}  "
          f"{qd:>9,.0f} шт  {rd:>14,.0f} ₽ за 12 мес")
    print(f"  наследование родителю  : {len(inherited):>5}  "
          f"{qi:>9,.0f} шт  {ri:>14,.0f} ₽ за 12 мес")
    print(f"  ИТОГО выручки под этими карточками: {rd + ri:,.0f} ₽ за 12 мес")
    print("  источник эталона: " + ", ".join(f"{k} {v}" for k, v in
                                             sorted(src.items(), key=lambda x: -x[1])))
    print(f"\nНЕ закрывается: {len(holes) - len(covered)}")
    for k, v in sorted(skipped.items(), key=lambda x: -x[1]):
        print(f"  {v:>6}  {k}")
    print("\nсписок:", OUT)


if __name__ == "__main__":
    main()
