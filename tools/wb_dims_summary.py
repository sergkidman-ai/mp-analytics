#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: gab
"""Сводка по прогону харвестера WB: сколько моделей закрыто, сколько выручки,
что осталось в очереди, и сверка метода с эталоном Icecat.

  python3 -m tools.wb_dims_summary --dir docs/web_search_v2/competitor_wb
"""
from __future__ import annotations
import argparse
import csv
import io
import os
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELLING = os.path.join(ROOT, "docs", "selling_uncovered_models.csv")
ICECAT = [os.path.join(ROOT, "docs", "web_search_v2", "hunt", b, "validation", "confirmed.csv")
          for b in ("batch_001", "batch_002")]


def read(path, delim=";"):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=delim))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    d = args.dir if os.path.isabs(args.dir) else os.path.join(ROOT, args.dir)

    harvest = read(os.path.join(d, "competitor_dims.csv"))
    conf = read(os.path.join(d, "confirmed.csv"))
    rej = read(os.path.join(d, "rejected.csv"))
    selling = read(SELLING)

    # выручка по (артикул-мать, OEM); ключ дублируется — берём по артикулу-матери
    rev = {}
    for r in selling:
        try:
            rev[r["семья_мать"]] = float(r["выручка_год_₽"])
        except (KeyError, ValueError):
            pass
    total_rev = sum(rev.values())

    print(f"ИСХОДНО непокрытых моделей: {len(rev)}, выручка {total_rev:,.0f} ₽"
          .replace(",", " "))

    # что вообще просмотрено
    seen = {r["vendorCode"] for r in harvest}
    st = Counter(r["status"] for r in harvest if r.get("competitor_article"))
    nf = {r["vendorCode"] for r in harvest if r["status"] == "NOT_FOUND"}
    print(f"\nХАРВЕСТЕР: просмотрено позиций {len(seen)}, строк-карточек "
          f"{sum(1 for r in harvest if r.get('competitor_article'))}")
    print(f"  из них CANDIDATE-строк {st.get('CANDIDATE', 0)}, "
          f"AMBIGUOUS {st.get('AMBIGUOUS', 0)}; без карточек с размерами {len(nf)} позиций")

    # подтверждено
    conf_vc = {r["vendorCode"] for r in conf}
    conf_rev = sum(rev.get(vc, 0.0) for vc in conf_vc)
    print(f"\nПОДТВЕРЖДЕНО (два разных продавца + перечитка карточки): "
          f"{len(conf_vc)} моделей")
    print(f"  выручка: {conf_rev:,.0f} ₽ = {conf_rev / total_rev * 100:.2f} % непокрытой"
          .replace(",", " "))

    # ранее закрытое через Icecat
    ice = {}
    for p in ICECAT:
        for r in read(p):
            if r.get("validation_status") == "CONFIRMED":
                ice[r["vendorCode"]] = r.get("dimensions_cm", "")
    ice_rev = sum(rev.get(vc, 0.0) for vc in ice)
    both = conf_vc | set(ice)
    both_rev = sum(rev.get(vc, 0.0) for vc in both)
    print(f"\nРанее закрыто через Icecat: {len(ice)} моделей, {ice_rev:,.0f} ₽"
          .replace(",", " "))
    print(f"ВСЕГО закрыто: {len(both)} моделей, {both_rev:,.0f} ₽ = "
          f"{both_rev / total_rev * 100:.2f} % непокрытой выручки".replace(",", " "))

    # сверка метода с эталоном: модели, закрытые обоими способами
    overlap = sorted(conf_vc & set(ice))
    if overlap:
        print(f"\nСВЕРКА МЕТОДА с эталоном Icecat — {len(overlap)} общих моделей:")
        cmap = {r["vendorCode"]: r for r in conf}
        for vc in overlap:
            c = cmap[vc]
            wb = sorted((float(c["длина_см"]), float(c["ширина_см"]), float(c["высота_см"])),
                        reverse=True)
            ic = sorted((float(x) for x in
                         re.findall(r"\d+(?:[.,]\d+)?", ice[vc].replace(",", "."))[:3]),
                        reverse=True)
            if len(ic) < 3:
                continue
            dev = max(abs(a - b) / b * 100 for a, b in zip(wb, ic))
            volw = wb[0] * wb[1] * wb[2] / 1000
            voli = ic[0] * ic[1] * ic[2] / 1000
            print(f"  {vc} {c['oem']:<14} ВБ {wb} vs Icecat {ic} — "
                  f"максимальное расхождение стороны {dev:.0f} %, "
                  f"объём {volw:.1f} л против {voli:.1f} л")

    # причины отбраковки
    if rej:
        print("\nОТБРАКОВКА (строк):")
        for reason, n in Counter(r["причина"] for r in rej).most_common():
            print(f"  {reason:<16} {n}")

    # очередь: что осталось
    left = {vc: rv for vc, rv in rev.items() if vc not in both}
    left_top = sorted(left.items(), key=lambda kv: -kv[1])[:15]
    print(f"\nОСТАЁТСЯ: {len(left)} моделей, {sum(left.values()):,.0f} ₽. Верх очереди:"
          .replace(",", " "))
    oem_by_vc = {r["семья_мать"]: r["OEM_модель"] for r in selling}
    for vc, rv in left_top:
        why = "не нашлось карточек" if vc in nf else (
            "продавцы не сошлись" if vc in seen else "не просмотрено")
        print(f"  {vc}  {oem_by_vc.get(vc, ''):<22} {rv:>10,.0f} ₽   {why}"
              .replace(",", " "))

    # файл очереди
    qpath = os.path.join(d, "queue_left.csv")
    with io.open(qpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["vendorCode", "oem", "выручка_год_₽", "почему_не_закрыто"])
        for vc, rv in sorted(left.items(), key=lambda kv: -kv[1]):
            why = "не нашлось карточек с размерами" if vc in nf else (
                "продавцы не сошлись / отбраковано" if vc in seen else "не просмотрено")
            w.writerow([vc, oem_by_vc.get(vc, ""), int(rv), why])
    print(f"\nОчередь записана: {qpath}")


if __name__ == "__main__":
    main()
