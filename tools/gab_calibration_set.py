# поток: gab
"""Сборка эталонной выборки для калибровки харвестера габаритов с карточек ВБ.

Эталон берётся ТОЛЬКО из источников вне интернет-карточек:
  * замеры Solutions Print (supplier_dims, supplier='solutionsprint') — короб поставщика;
  * консенсус >=2 разных «чистых» поставщиков (изи / cactus / sakura / solutionsprint),
    у которых стороны сходятся в пределах max(10 %, 1,5 см);
  * подтверждённые паспортные данные Icecat (docs/web_search_v2/hunt/batch_*/validation).

Модель опознаётся по ПОЛНОМУ OEM-коду из названия товара (whitelist из
GABARITY_SOLUTIONSPRINT_HANDOFF); названия, где кодов ноль или больше одного, отбрасываются —
неоднозначный ключ хуже отсутствующего.

На выходе:
  calibration_truth.csv — эталон (oem;длина;ширина;высота;объём;источник;подробность)
  calibration_input.csv — вход харвестера в его штатном формате (vendorCode;oem;...)
"""
import csv
import os
import re
import sys
from collections import defaultdict

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, "docs", "web_search_v2", "calibration")

# Поставщики, чьи цифры — свои замеры/паспорт короба, а не выдача интернет-карточек.
# rapid и profiline исключены: rapid — сборка со стороннего API, profiline без размеров.
CLEAN = ("solutionsprint", "изи", "cactus", "sakura")

FAM = [
    r"\d{3}R\d{4,5}", r"MLT-?D\d{3}[A-Z]?", r"CLT-?[KCMY]\d{3}[A-Z]?", r"C-?EXV\d{1,3}",
    r"[CG]PR-?\d{1,3}", r"CRG-?\d{2,3}[A-Z]{0,2}", r"TN-?\d{3,4}[A-Z]{0,2}",
    r"DR-?\d{3,4}[A-Z]{0,2}", r"TK-?\d{3,4}[A-Z]?", r"DK-?\d{3,4}", r"C[EFB]\d{3}[AXYUD]?",
    r"W[12]\d{3}[AX]?", r"C[CN]\d{3}[A-Z]?", r"Q\d{4}[AX]?",
    r"C13[A-Z]\d{2}[A-Z0-9]\d{2}[A-Z]?", r"106R\d{5}|108R\d{5}|013R\d{5}|101R\d{5}",
]
RX = re.compile(r"(?<![A-Z0-9])(?:%s)(?![A-Z0-9])" % "|".join(FAM), re.I)

MAX_VOL_L = 25.0          # больше — мастер-короб, не единичная упаковка
MIN_SIDE, MAX_SIDE = 1.0, 120.0


def norm(code):
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def one_oem(text):
    """Ровно один полный OEM-код в названии — иначе ключ неоднозначен."""
    codes = {norm(m) for m in RX.findall((text or "").upper()) if len(norm(m)) >= 5}
    return codes.pop() if len(codes) == 1 else None


def sane(l, w, h, vol):
    sides = (float(l), float(w), float(h))
    if any(s < MIN_SIDE or s > MAX_SIDE for s in sides):
        return False
    calc = sides[0] * sides[1] * sides[2] / 1000.0
    if calc > MAX_VOL_L:
        return False
    # объём из прайса должен биться с произведением сторон — иначе строка битая
    if vol and abs(float(vol) - calc) > 0.15 * calc:
        return False
    return True


def close(a, b):
    """Стороны считаем совпавшими, если расхождение в пределах 10 % или 1,5 см."""
    return abs(a - b) <= max(0.10 * max(a, b), 1.5)


def main():
    load_dotenv(os.path.join(ROOT, ".env"))
    dsn = os.getenv("DATABASE_URL") or os.getenv("PG_DSN")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(
        "select supplier, article, title, length_cm, width_cm, height_cm, volume_l "
        "from supplier_dims where supplier = any(%s) and length_cm is not null "
        "and width_cm is not null and height_cm is not null", (list(CLEAN),))

    # oem -> supplier -> список отсортированных троек сторон
    by_oem = defaultdict(lambda: defaultdict(list))
    seen_titles = 0
    for supplier, article, title, l, w, h, vol in cur.fetchall():
        seen_titles += 1
        oem = one_oem(title) or one_oem(article)
        if not oem or not sane(l, w, h, vol):
            continue
        by_oem[oem][supplier].append(
            (tuple(sorted((float(l), float(w), float(h)), reverse=True)), article, title))
    print(f"строк поставщиков прочитано: {seen_titles}, моделей с однозначным OEM: {len(by_oem)}")

    truth = {}

    # --- 1. Консенсус >=2 чистых поставщиков -------------------------------------
    for oem, per_sup in by_oem.items():
        # внутри поставщика оставляем только согласованных с собой (иначе цвет/ёмкость смешались)
        reps = {}
        for sup, items in per_sup.items():
            base = items[0][0]
            if all(all(close(a, b) for a, b in zip(base, it[0])) for it in items):
                reps[sup] = items[0]
        if len(reps) < 2:
            continue
        sups = sorted(reps)
        base = reps[sups[0]][0]
        agree = [s for s in sups if all(close(a, b) for a, b in zip(base, reps[s][0]))]
        if len(agree) < 2:
            continue
        dims = tuple(round(sum(reps[s][0][i] for s in agree) / len(agree), 1) for i in range(3))
        truth[oem] = {
            "dims": dims, "source": "консенсус-поставщиков",
            "detail": "; ".join(f"{s}:{'x'.join(str(x) for x in reps[s][0])}" for s in agree),
        }

    # --- 2. Замеры Solutions Print (по одному согласованному коробу на модель) ----
    sp_only = 0
    for oem, per_sup in by_oem.items():
        if oem in truth or "solutionsprint" not in per_sup:
            continue
        items = per_sup["solutionsprint"]
        base = items[0][0]
        if not all(all(close(a, b) for a, b in zip(base, it[0])) for it in items):
            continue
        truth[oem] = {
            "dims": base, "source": "замер-SolutionsPrint",
            "detail": f"код {items[0][1]}: {items[0][2][:70]}",
        }
        sp_only += 1

    # --- 3. Паспорт Icecat -------------------------------------------------------
    ice = 0
    for batch in ("batch_001", "batch_002"):
        path = os.path.join(ROOT, "docs", "web_search_v2", "hunt", batch,
                            "validation", "confirmed.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r.get("validation_status") != "CONFIRMED":
                    continue
                nums = re.findall(r"\d+(?:[.,]\d+)?", (r.get("dimensions_cm") or "").replace(",", "."))
                if len(nums) < 3:
                    continue
                dims = tuple(sorted((float(x) for x in nums[:3]), reverse=True))
                oem = norm(r.get("oem_code") or r.get("model") or "")
                if not oem:
                    continue
                truth[oem] = {  # паспорт производителя перекрывает поставщика
                    "dims": dims, "source": "паспорт-Icecat",
                    "detail": f"{r.get('model')} / {r.get('original_dimensions')}",
                }
                ice += 1

    print(f"эталон: всего {len(truth)} — консенсус "
          f"{sum(1 for v in truth.values() if v['source'] == 'консенсус-поставщиков')}, "
          f"SolutionsPrint {sp_only}, Icecat {ice}")

    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "calibration_truth.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["oem", "длина_см", "ширина_см", "высота_см", "объём_л", "источник", "подробность"])
        for oem in sorted(truth):
            d = truth[oem]["dims"]
            w.writerow([oem, d[0], d[1], d[2], round(d[0] * d[1] * d[2] / 1000, 3),
                        truth[oem]["source"], truth[oem]["detail"]])
    print("записан", os.path.join(OUTDIR, "calibration_truth.csv"))
    return truth


if __name__ == "__main__":
    main()
