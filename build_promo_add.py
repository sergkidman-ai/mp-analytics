"""build_promo_add.py — финал: решение по акции на СЕГОДНЯШНЕЙ себестоимости + файл на добавление.

Себест/шт = min buy_price с «Удалённого склада» за сегодня (свежий прайс поставщика); где товара
там нет — fallback на витрину margin_by_sku (по nm_id). net@план = плановая*K - логистика - себест.
Решаем по ВСЕМУ файлу saleout.xlsx (не только продающие). → docs/promo_decision_today.txt
+ incoming/promo_add.xlsx (зелёные: мин.цена = плановая, готово к загрузке).
"""
import sys
import pathlib
import pandas as pd

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

XLSX = BASE_DIR / "incoming" / "saleout.xlsx"
OUT = BASE_DIR / "docs" / "promo_decision_today.txt"
ADD = BASE_DIR / "incoming" / "promo_add.xlsx"
ACC = "wb_acc1"
COMM, RET, LOGIST = 0.132, 0.025, 293
K = 1 - COMM - RET
TARGET_NET = 400

C = {"nm": "Артикул WB", "vc": "Артикул поставщика", "plan": "Плановая цена для акции",
     "cur": "Текущая розничная цена", "minauto": "Минимальная цена для применения скидки по автоакции",
     "part": "Товар уже участвует в акции", "st": "Статус", "stockwb": "Остаток товара на складах Wb (шт.)"}


def w(f, s=""):
    f.write(s + "\n")


def main():
    df = pd.read_excel(XLSX)

    # СЕБЕСТ из МойСклад (закупочная, ежедневный прайс): баркод → внешний код. Витрина — fallback.
    ms_bc = {r["barcode"]: float(r["buy_price"]) for r in db.query(
        "SELECT b.barcode, p.buy_price FROM ms_barcode b JOIN ms_product p ON p.ms_id=b.ms_id "
        "WHERE p.buy_price>0")}
    ms_ext = {r["external_code"]: float(r["bp"]) for r in db.query(
        "SELECT external_code, min(buy_price) bp FROM ms_product WHERE buy_price>0 AND external_code IS NOT NULL "
        "GROUP BY external_code")}
    vitrina = {int(r["nm_id"]): float(r["seb"]) for r in db.query(
        "SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb "
        "FROM margin_by_sku WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$' "
        "ORDER BY article, period_from DESC", (ACC,)) if r["seb"]}
    rev = {int(r["nm_id"]): float(r["order_sum"] or 0) for r in db.query(
        "SELECT nm_id, order_sum FROM wb_funnel WHERE account=%s AND period='2026-06-01' AND order_count>0", (ACC,))}

    def _bc(x):
        if pd.isna(x):
            return None
        s = str(x)
        return s[:-2] if s.endswith(".0") else s

    rows = []
    src_bc = src_ext = src_vitr = src_none = 0
    for _, r in df.iterrows():
        nm = r[C["nm"]]
        plan = r[C["plan"]]
        if pd.isna(nm) or pd.isna(plan):
            continue
        nm = int(nm)
        vc = None if pd.isna(r[C["vc"]]) else str(r[C["vc"]]).strip()
        bc = _bc(r["Последний баркод"])
        cost = ms_bc.get(bc)
        if cost is not None:
            src = "мс-бк"; src_bc += 1
        elif vc and ms_ext.get(vc) is not None:
            cost = ms_ext[vc]; src = "мс-ек"; src_ext += 1
        elif nm in vitrina:
            cost = vitrina[nm]; src = "витр"; src_vitr += 1
        else:
            src = None; src_none += 1
        net = round(float(plan) * K - LOGIST - cost) if cost is not None else None
        rows.append({"nm": nm, "vc": vc, "plan": float(plan),
                     "cur": None if pd.isna(r[C["cur"]]) else float(r[C["cur"]]),
                     "st": str(r[C["st"]]), "cost": cost, "src": src, "net": net,
                     "os": rev.get(nm, 0.0)})

    have = [r for r in rows if r["net"] is not None]
    green = [r for r in have if r["net"] >= TARGET_NET]
    thin = [r for r in have if 0 <= r["net"] < TARGET_NET]
    neg = [r for r in have if r["net"] < 0]

    def f0(x):
        return float(x or 0)

    # файл на добавление: зелёные → мин.цена = плановая (точно входим в акцию с маржой)
    add_df = df[df[C["nm"]].isin({r["nm"] for r in green})].copy()
    add_df[C["minauto"]] = add_df[C["plan"]]      # ставим мин.цену = плановой
    add_df.to_excel(ADD, index=False)

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "РЕШЕНИЕ ПО АКЦИИ НА СЕГОДНЯШНЕЙ СЕБЕСТОИМОСТИ (закупочная МойСклад: баркод/код, + витрина)")
        w(f, "=" * 96)
        w(f, f"net@план = плановая*{K:.3f} - {LOGIST} - себест(закупочная МС). Цель ≥{TARGET_NET}₽/шт.")
        sellers_n = sum(1 for r in rows if r["os"] > 0)
        sellers_cov = sum(1 for r in rows if r["os"] > 0 and r["net"] is not None)
        w(f, f"Строк с плановой: {len(rows)} | себест найдена: {len(have)} "
             f"(МС-баркод {src_bc}, МС-код {src_ext}, витрина {src_vitr}) | без себеста: {src_none}")
        w(f, f"ПОКРЫТИЕ ПРОДАЮЩИХ (деньги): {sellers_cov}/{sellers_n} "
             f"({round(sellers_cov/sellers_n*100) if sellers_n else 0}%) — остальное мёртвый WB-хвост.")
        w(f, "")
        w(f, "РЕШЕНИЕ:")
        w(f, f"  🟢 ДОБАВИТЬ с маржой (net@план ≥{TARGET_NET}): {len(green):>4} SKU | выручка-под {sum(f0(r['os']) for r in green):,.0f} ₽")
        w(f, f"  🟡 ТОНКО (0..{TARGET_NET}):                    {len(thin):>4} SKU | {sum(f0(r['os']) for r in thin):,.0f} ₽ (герои в 0 / срезать себест)")
        w(f, f"  🔴 НЕ ДОБАВЛЯТЬ (net@план <0):               {len(neg):>4} SKU | {sum(f0(r['os']) for r in neg):,.0f} ₽")
        w(f, "")
        w(f, f"ФАЙЛ НА ЗАГРУЗКУ: incoming/promo_add.xlsx — {len(add_df)} зелёных SKU, мин.цена = плановой.")
        w(f, "")
        w(f, "ТОП-30 ЗЕЛЁНЫХ ПО ВЫРУЧКЕ:")
        w(f, f"  {'nmID':>11} {'выр':>8} {'план.ц':>6} {'себест':>6} {'ист':>5} {'net@план':>8}")
        w(f, "  " + "-" * 60)
        for r in sorted(green, key=lambda r: -f0(r["os"]))[:30]:
            w(f, f"  {r['nm']:>11} {f0(r['os']):>8,.0f} {int(r['plan']):>6} {int(r['cost']):>6} {r['src']:>5} {r['net']:>8}")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} + {ADD} | 🟢{len(green)} 🟡{len(thin)} 🔴{len(neg)} | "
          f"себест: МС-бк {src_bc} МС-ек {src_ext} витр {src_vitr} нет {src_none}")


if __name__ == "__main__":
    main()
