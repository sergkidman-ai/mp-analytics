"""promo_decision.py — решение по акции на РЕАЛЬНЫХ плановых ценах WB (saleout.xlsx из ЛК).

«Плановая цена для акции» = проходная (нужно ≤ неё, чтобы участвовать+буст). «Минимальная цена» = наш пол.
Считаем net@плановая = плановая*K - логистика - себест (себест из margin_by_sku по nm_id). Решаем по SKU:
участвовать с маржой / тонко / в 0 ради скорости / не входить (плановая ниже нашей экономики).
→ docs/promo_decision.txt
"""
import sys
import pathlib
import pandas as pd

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

XLSX = BASE_DIR / "incoming" / "saleout.xlsx"
OUT = BASE_DIR / "docs" / "promo_decision.txt"
ACC = "wb_acc1"
COMM, RET, LOGIST = 0.132, 0.025, 293
K = 1 - COMM - RET
TARGET_NET = 400

C = {"nm": "Артикул WB", "plan": "Плановая цена для акции", "cur": "Текущая розничная цена",
     "minauto": "Минимальная цена для применения скидки по автоакции",
     "part": "Товар уже участвует в акции", "st": "Статус",
     "stockwb": "Остаток товара на складах Wb (шт.)"}


def net_unit(price, cogs):
    if price is None or cogs is None:
        return None
    return round(float(price) * K - LOGIST - float(cogs))


def w(f, s=""):
    f.write(s + "\n")


def main():
    df = pd.read_excel(XLSX)
    promo = {}
    for _, r in df.iterrows():
        nm = r[C["nm"]]
        if pd.isna(nm):
            continue
        promo[int(nm)] = {
            "plan": None if pd.isna(r[C["plan"]]) else float(r[C["plan"]]),
            "cur": None if pd.isna(r[C["cur"]]) else float(r[C["cur"]]),
            "minauto": None if pd.isna(r[C["minauto"]]) else float(r[C["minauto"]]),
            "part": r[C["part"]], "st": str(r[C["st"]]),
            "stock": 0 if pd.isna(r[C["stockwb"]]) else int(r[C["stockwb"]]),
        }

    cogs = {int(r["nm_id"]): float(r["seb"]) for r in db.query(
        "SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb "
        "FROM margin_by_sku WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$' "
        "ORDER BY article, period_from DESC", (ACC,)) if r["seb"]}
    rev = {int(r["nm_id"]): (float(r["order_sum"] or 0), r["order_count"]) for r in db.query(
        "SELECT nm_id, order_sum, order_count FROM wb_funnel WHERE account=%s AND period='2026-06-01' AND order_count>0", (ACC,))}
    pos = {int(r["nm_id"]): r for r in db.query(
        "SELECT nm_id, pos, pos_dyn, open_dyn FROM wb_jam_may WHERE account=%s", (ACC,))}

    def f0(x):
        return float(x or 0)

    # вселенная решения: наши ПРОДАЮЩИЕ SKU (деньги) с плановой ценой и себестом
    recs = []
    for nm, p in promo.items():
        if nm not in rev or not p["plan"]:
            continue
        cg = cogs.get(nm)
        r_os, r_oc = rev[nm]
        pp = pos.get(nm) or {}
        recs.append({"nm": nm, "plan": p["plan"], "cur": p["cur"], "minauto": p["minauto"],
                     "part": p["part"], "st": p["st"], "stock": p["stock"], "cogs": cg,
                     "os": r_os, "oc": r_oc, "pos": pp.get("pos"),
                     "dropped": (pp.get("pos_dyn") or 0) > 0 or (pp.get("open_dyn") or 0) < 0,
                     "net_plan": net_unit(p["plan"], cg)})

    # сегментация по net@плановая
    part_in = [r for r in recs if str(r["part"]).strip() == "Да"]
    out_minhigh = [r for r in recs if "минимальная цена выше плановой" in r["st"]]
    with_cogs = [r for r in recs if r["cogs"]]
    green = [r for r in with_cogs if r["net_plan"] is not None and r["net_plan"] >= TARGET_NET]
    thin = [r for r in with_cogs if r["net_plan"] is not None and 0 <= r["net_plan"] < TARGET_NET]
    neg = [r for r in with_cogs if r["net_plan"] is not None and r["net_plan"] < 0]

    def tot(lst):
        return sum(f0(r["os"]) for r in lst)

    def tbl(f, lst, n=30):
        w(f, f"  {'nmID':>11} {'выр.июнь':>9} {'поз':>4} {'тек.ц':>6} {'план.ц':>6} {'-скидка':>7} "
             f"{'себест':>6} {'net@план':>8} {'участв':>7}  товар")
        w(f, "  " + "-" * 104)
        for r in sorted(lst, key=lambda r: -f0(r["os"]))[:n]:
            disc = f"-{round((r['cur']-r['plan'])/r['cur']*100)}%" if r["cur"] else "—"
            part = "ДА" if str(r["part"]).strip() == "Да" else "нет"
            nm_name = "?"
            w(f, f"  {r['nm']:>11} {f0(r['os']):>9,.0f} {str(r['pos'] or '—'):>4} "
                 f"{int(r['cur']) if r['cur'] else '—':>6} {int(r['plan']):>6} {disc:>7} "
                 f"{int(r['cogs']):>6} {str(r['net_plan']):>8} {part:>7}")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "РЕШЕНИЕ ПО АКЦИИ — на РЕАЛЬНЫХ плановых ценах WB (saleout.xlsx)")
        w(f, "=" * 100)
        w(f, "Плановая цена = проходная WB (нужно ≤ неё). net@план = чистая ₽/шт при плановой цене")
        w(f, f"(net=план*{K:.3f}-{LOGIST}-себест). Себест из margin_by_sku по nm_id.")
        w(f, "")
        w(f, "1. ОБЩАЯ КАРТИНА ПО ПРОДАЮЩИМ (наши SKU с продажами и плановой ценой)")
        w(f, f"   Всего продающих в отчёте: {len(recs)} | с себестом: {len(with_cogs)}")
        w(f, f"   Уже участвуют: {len(part_in)} | НЕ участвуют «мин.цена выше плановой» (мы сами вышли): {len(out_minhigh)}")
        w(f, "")
        w(f, "2. РЕШЕНИЕ ПО ПЛАНОВОЙ ЦЕНЕ (net@план):")
        w(f, f"   🟢 УЧАСТВОВАТЬ с маржой (net@план ≥{TARGET_NET}₽): {len(green):>3} SKU | выручка {tot(green):,.0f} ₽")
        w(f, f"   🟡 ТОНКО (net@план 0..{TARGET_NET}₽):            {len(thin):>3} SKU | выручка {tot(thin):,.0f} ₽ — герои в 0 ради скорости / срезать себест")
        w(f, f"   🔴 НЕ ВХОДИТЬ (net@план <0, плановая ниже экономики): {len(neg):>3} SKU | выручка {tot(neg):,.0f} ₽ — держать цену, не лезть")
        w(f, "")
        w(f, f"3. 🟢 УЧАСТВОВАТЬ С МАРЖОЙ — снизить мин.цену до плановой, получить буст (топ-30 по деньгам из {len(green)})")
        tbl(f, green)
        w(f, "")
        w(f, f"4. 🟡 ТОНКО / КАНДИДАТЫ «В 0» РАДИ СКОРОСТИ — топ-25 по деньгам из {len(thin)}")
        w(f, "   Эти герои на плановой дают почти 0 — сознательно входим на горстке топов ради ранга/лояльности.")
        tbl(f, thin, 25)
        w(f, "")
        w(f, f"5. 🔴 НЕ ВХОДИТЬ ЦЕНОЙ — плановая ниже нашей экономики (топ-20 по деньгам из {len(neg)})")
        w(f, "   Тут участие = убыток. Держим цену, продаём одиночно дороже, или режем себест/исключаем.")
        tbl(f, neg, 20)
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | продающих {len(recs)} | 🟢{len(green)} 🟡{len(thin)} 🔴{len(neg)} | "
          f"вышли сами (min>план): {len(out_minhigh)}")


if __name__ == "__main__":
    main()
