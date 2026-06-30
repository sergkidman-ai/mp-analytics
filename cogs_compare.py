"""cogs_compare.py — себестоимость рядом: computed (дашборд, margin_by_sku) vs файл (отчёты МС).

Не меняет витрину. Показывает по Цифровому (acc1), помесячно, по МП:
  выручка(наша цена) | COGS дашборд | COGS файл | Δ | влияние на чистую (= дашборд_cogs − файл_cogs).
→ docs/cogs_compare.txt
"""
import sys
import pathlib
from core import db  # noqa

BASE = pathlib.Path(__file__).resolve().parent
OUT = BASE / "docs" / "cogs_compare.txt"

# Авторитетные числа из expenses.xlsx (Цифровой), помесячно
FILE = {
    "Озон":   {"cogs": {"01":1943106,"02":1871670,"03":2447160,"04":2369085,"05":2390892}},
    "ВБ":     {"cogs": {"01":946376,"02":1284369,"03":1113371,"04":852235,"05":None},
               "rev_ourprice": {"01":3847505,"02":4171696,"03":3570813,"04":3377295,"05":None}},
    "Маркет": {"cogs": {"01":204954,"02":158009,"03":229849,"04":358191,"05":530357}},
}
MO = ["01","02","03","04","05"]
MN = {"01":"Янв","02":"Фев","03":"Мар","04":"Апр","05":"Май"}


def dash_by_month(platform):
    out = {}
    for r in db.query("""
        SELECT to_char(period_from,'MM') mm, round(sum(cogs)) cogs, round(sum(net_profit)) net,
               round(sum(revenue_buyer)) rev
        FROM margin_by_sku WHERE platform=%s AND account=%s GROUP BY 1""",
        (platform, "wb_acc1" if platform == "wb" else "oz_acc1")):
        out[r["mm"]] = r
    return out


def f(x):
    return f"{int(x):,}" if x is not None else "—"


def main():
    dash = {"Озон": dash_by_month("ozon"), "ВБ": dash_by_month("wb")}
    lines = []
    def w(s=""): lines.append(s)

    w("СЕБЕСТОИМОСТЬ РЯДОМ — дашборд (computed) vs таблица (отчёты МС). Цифровой. Витрину НЕ меняем.")
    w("Δ COGS = файл − дашборд. Влияние на чистую = дашборд_cogs − файл_cogs (если COGS файла выше → чистая ниже).")
    w("=" * 92)
    for mp in ("Озон", "ВБ"):
        w(f"\n● {mp}")
        w(f"  {'мес':4} {'COGS дашборд':>14} {'COGS файл':>12} {'Δ файл−даш':>12} {'Δ%':>7} {'→чистая, ₽':>12}")
        w("  " + "-" * 78)
        for m in MO:
            fc = FILE[mp]["cogs"].get(m)
            d = dash[mp].get(m, {})
            dc = d.get("cogs")
            if dc is None and fc is None:
                continue
            if fc is None:
                w(f"  {MN[m]:4} {f(dc):>14} {'—':>12} {'(нет в файле)':>12}")
                continue
            delta = fc - (dc or 0)
            pct = delta / dc * 100 if dc else 0
            net_eff = (dc or 0) - fc  # на столько изменится чистая, если взять файл-COGS
            w(f"  {MN[m]:4} {f(dc):>14} {f(fc):>12} {delta:>+12,} {pct:>+6.1f}% {net_eff:>+12,}")
    w("\n● Маркет — в витрине дашборда COGS нет (Yandex не помесячно). Файл (справочно):")
    w("  " + " | ".join(f"{MN[m]} {f(FILE['Маркет']['cogs'][m])}" for m in MO))
    w("\nИтоги Jan-Apr (Озон+ВБ): "
      f"COGS дашборд {f(sum((dash['Озон'].get(m,{}).get('cogs') or 0)+(dash['ВБ'].get(m,{}).get('cogs') or 0) for m in MO[:4]))}"
      f" | COGS файл {f(sum(FILE['Озон']['cogs'][m]+FILE['ВБ']['cogs'][m] for m in MO[:4]))}")
    w("\n— конец —")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[saved] {OUT}")


if __name__ == "__main__":
    main()
