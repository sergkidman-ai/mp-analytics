# поток: gab
"""Калибровка харвестера ВБ: сравнение того, что сказали карточки конкурентов, с эталоном.

Вход:
  docs/web_search_v2/calibration/calibration_key.csv  — эталон (vendorCode -> реальные стороны)
  docs/web_search_v2/calibration/validation/confirmed.csv — вердикт независимого валидатора
  docs/web_search_v2/competitor_wb/confirmed.csv     — боевой прогон, нужен для частот коробок

Считает: долю попаданий в ±10 % и в ±1,5 см, медианную и максимальную ошибку по сторонам и по
объёму, и отдельно — сколько «коробок по умолчанию» проходит фильтр редкости.
"""
import csv
import os
import re
import statistics as st
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = os.path.join(ROOT, "docs", "web_search_v2", "calibration")
MAIN = os.path.join(ROOT, "docs", "web_search_v2", "competitor_wb", "confirmed.csv")

TOL_PCT = 0.10
TOL_CM = 1.5
MASS_MIN = 5      # коробка у >=5 моделей боевого прогона — массовка
RARE_MAX = 2      # фильтр редкости из отчёта 16: коробка встречается у <=2 моделей


def rd(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def sides(r, keys=("длина_см", "ширина_см", "высота_см")):
    return tuple(sorted((float(r[k].replace(",", ".")) for k in keys), reverse=True))


def triple(s):
    return "x".join(f"{x:g}" for x in s)


def main():
    truth = {r["vendorCode"]: r for r in rd(os.path.join(CAL, "calibration_key.csv"))}
    conf = rd(os.path.join(CAL, "validation", "confirmed.csv"))

    # частоты коробок берём из боевого прогона — фильтр редкости работал бы именно на нём
    freq = Counter()
    if os.path.exists(MAIN):
        for r in rd(MAIN):
            freq[triple(sides(r))] += 1

    rows = []
    for r in conf:
        t = truth.get(r["vendorCode"])
        if not t:
            continue
        wb = sides(r)
        tr = sides(t)
        err_cm = [abs(a - b) for a, b in zip(wb, tr)]
        err_pct = [abs(a - b) / b * 100 for a, b in zip(wb, tr)]
        vw = wb[0] * wb[1] * wb[2] / 1000
        vt = tr[0] * tr[1] * tr[2] / 1000
        box = triple(wb)
        rows.append({
            "vendorCode": r["vendorCode"], "oem": r["oem"], "источник_эталона": t["источник"],
            "вб": triple(wb), "эталон": triple(tr),
            "объём_вб_л": round(vw, 2), "объём_эталон_л": round(vt, 2),
            "ошибка_объём_%": round((vw - vt) / vt * 100, 1),
            "макс_ошибка_стороны_%": round(max(err_pct), 1),
            "макс_ошибка_стороны_см": round(max(err_cm), 1),
            "в_допуске_10%": all(e <= TOL_PCT * 100 for e in err_pct),
            "в_допуске_1.5см": all(e <= TOL_CM for e in err_cm),
            "коробка_частота_в_боевом": freq.get(box, 0),
            "редкая": freq.get(box, 0) <= RARE_MAX,
            "массовка": freq.get(box, 0) >= MASS_MIN,
            "_err_pct": err_pct, "_err_cm": err_cm,
        })

    if not rows:
        print("нет пересечения подтверждённых с эталоном")
        return

    def block(title, sub):
        if not sub:
            print(f"\n## {title}: пусто")
            return
        n = len(sub)
        all_pct = [e for r in sub for e in r["_err_pct"]]
        all_cm = [e for r in sub for e in r["_err_cm"]]
        vol = [abs(r["ошибка_объём_%"]) for r in sub]
        hit10 = sum(r["в_допуске_10%"] for r in sub)
        hit15 = sum(r["в_допуске_1.5см"] for r in sub)
        hit_any = sum(r["в_допуске_10%"] or r["в_допуске_1.5см"] for r in sub)
        over = sum(1 for r in sub if r["ошибка_объём_%"] > 20)
        under = sum(1 for r in sub if r["ошибка_объём_%"] < -20)
        print(f"\n## {title}: {n} моделей")
        print(f"  все 3 стороны в ±10 %      : {hit10} ({hit10/n*100:.0f} %)")
        print(f"  все 3 стороны в ±1,5 см    : {hit15} ({hit15/n*100:.0f} %)")
        print(f"  в ±10 % ИЛИ ±1,5 см        : {hit_any} ({hit_any/n*100:.0f} %)")
        print(f"  ошибка стороны, %   медиана {st.median(all_pct):.1f}  макс {max(all_pct):.0f}")
        print(f"  ошибка стороны, см  медиана {st.median(all_cm):.1f}  макс {max(all_cm):.1f}")
        print(f"  ошибка объёма, %    медиана {st.median(vol):.1f}  макс {max(vol):.0f}")
        print(f"  завышений объёма >20 %: {over} ({over/n*100:.0f} %), "
              f"занижений <-20 %: {under} ({under/n*100:.0f} %)")

    block("ВСЕ подтверждённые", rows)
    block("Прошли фильтр редкости (коробка у ≤2 моделей боевого прогона)",
          [r for r in rows if r["редкая"]])
    block("Массовка (коробка у ≥5 моделей) — отбраковывается фильтром",
          [r for r in rows if r["массовка"]])
    for src in sorted({r["источник_эталона"] for r in rows}):
        block(f"эталон: {src}", [r for r in rows if r["источник_эталона"] == src])

    print("\n## Дефолтные коробки в подтверждениях калибровки")
    cnt = Counter(r["вб"] for r in rows)
    for box, n in cnt.most_common(8):
        sub = [r for r in rows if r["вб"] == box]
        ok = sum(r["в_допуске_10%"] or r["в_допуске_1.5см"] for r in sub)
        print(f"  {box:>14}  моделей {n:>3}  верных {ok:>3}  "
              f"частота в боевом {freq.get(box,0):>3}  "
              f"{'МАССОВКА' if freq.get(box,0)>=MASS_MIN else 'редкая' if freq.get(box,0)<=RARE_MAX else '—'}")

    rare = [r for r in rows if r["редкая"]]
    bad_rare = [r for r in rare if not (r["в_допуске_10%"] or r["в_допуске_1.5см"])]
    print(f"\n  через фильтр редкости прошло {len(rare)} моделей, "
          f"из них НЕ бьются с эталоном {len(bad_rare)} ({len(bad_rare)/max(len(rare),1)*100:.0f} %)")

    out = os.path.join(CAL, "calibration_compare.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        cols = [k for k in rows[0] if not k.startswith("_")]
        w = csv.DictWriter(f, cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: -abs(x["ошибка_объём_%"])):
            w.writerow(r)
    print("\nпострочно:", out)


if __name__ == "__main__":
    main()
