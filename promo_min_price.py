"""promo_min_price.py — решение по авто-акциям: минимальная цена per SKU (наш пол), не проходная.

Авто-акции WB: товар участвует автоматически, продавец задаёт МИН.ЦЕНУ (пол) и может исключить товар.
Проходного порога по API нет → рычаг = наша мин.цена. Считаем два ориентира на штуку:
  • breakeven  = (логистика+себест)/K  — цена при чистой 0 (для сознательного «в 0» ради скорости);
  • floor_marg = (target+логистика+себест)/K — цена при целевой марже (защита маржи).
Решение: где участвовать с маржой, где в 0 ради лояльности профиля, где исключить. → docs/promo_min_price.txt
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "promo_min_price.txt"
ACC = "wb_acc1"
COMM, RET, LOGIST = 0.132, 0.025, 293
K = 1 - COMM - RET
TARGET_NET = 400
VELOCITY_N = 12          # сколько топ-героев по деньгам ведём «в 0» ради перезапуска скорости


def price_for_net(target, cogs):
    return round((target + LOGIST + float(cogs)) / K)


def w(f, s=""):
    f.write(s + "\n")


def main():
    rows = db.query("""
        WITH cogs AS (
          SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb
          FROM margin_by_sku WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
          ORDER BY article, period_from DESC)
        SELECT j.nm_id, j.name, j.pos, j.price p_cur, j.is_advertised adv,
               f.order_count oc, f.order_sum os, cg.seb
        FROM wb_jam_may j
        JOIN wb_funnel f ON f.account=j.account AND f.nm_id=j.nm_id AND f.period='2026-06-01' AND f.order_count>0
        JOIN cogs cg ON cg.nm_id=j.nm_id
        WHERE j.account=%s AND (j.pos_dyn>0 OR j.open_dyn<0)
    """, (ACC, ACC))
    for r in rows:
        r["p_cur"] = float(r["p_cur"]) if r["p_cur"] is not None else None
        r["seb"] = float(r["seb"])
        r["be"] = price_for_net(0, r["seb"])         # цена при чистой 0
        r["fl"] = price_for_net(TARGET_NET, r["seb"])  # цена при целевой марже

    def f0(x):
        return float(x or 0)
    rows.sort(key=lambda r: -f0(r["os"]))

    # стратегия: топ-N по деньгам и на рекламе → «в 0» (перезапуск скорости/лояльности);
    # если даже breakeven выше текущей цены (себест душит) → исключить; иначе участвовать с маржой.
    velo, margin, excl = [], [], []
    velo_ids = set()
    for r in rows:
        if r["p_cur"] and r["be"] > r["p_cur"]:
            excl.append(r)                            # даже в 0 цена выше текущей — себест душит, не лезть
        elif len(velo) < VELOCITY_N and r["adv"]:
            velo.append(r); velo_ids.add(r["nm_id"])  # топ-герой на рекламе → в 0 ради скорости
        else:
            margin.append(r)

    def tbl(f, lst, cols_minprice):
        w(f, f"  {'nmID':>11} {'выр.июнь':>9} {'поз':>4} {'тек.ц':>6} {'себест':>6} {'в0(be)':>7} "
             f"{'марж.пол':>8} {'→мин.цена':>9} {'рекл':>5}  товар")
        w(f, "  " + "-" * 104)
        for r in lst:
            mp = r["be"] if cols_minprice == "be" else (r["fl"] if cols_minprice == "fl" else r["p_cur"])
            disc = f"-{round((r['p_cur']-mp)/r['p_cur']*100)}%" if (r["p_cur"] and mp < r["p_cur"]) else "—"
            w(f, f"  {r['nm_id']:>11} {f0(r['os']):>9,.0f} {str(r['pos'] or '—'):>4} "
                 f"{str(int(r['p_cur'])) if r['p_cur'] else '—':>6} {int(r['seb']):>6} {r['be']:>7} {r['fl']:>8} "
                 f"{int(mp):>6}{disc:>3} {('да' if r['adv'] else '—'):>5}  {(r['name'] or '')[:24]}")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "АВТО-АКЦИИ WB — РЕШЕНИЕ ПО МИН.ЦЕНЕ (пол), не по проходной (её нет)")
        w(f, "=" * 108)
        w(f, "МЕХАНИКА: товар в авто-акции участвует сам; мы задаём МИН.ЦЕНУ (пол) и можем исключить товар.")
        w(f, "WB скидывает в пределах до пола + бустинг до 35% в каталоге/поиске. Ниже пола НЕ продаём → нет риска.")
        w(f, f"be = цена при чистой 0 (перезапуск скорости); марж.пол = цена при чистой ≥{TARGET_NET}₽/шт (защита маржи).")
        w(f, f"Сейчас будущих акций по нашей категории: 10 авто (опт-ин regular в картриджах нет).")
        w(f, "")
        w(f, f"🔥 «В 0» РАДИ ЛОЯЛЬНОСТИ/СКОРОСТИ — топ-{len(velo)} героев на рекламе (мин.цена = breakeven)")
        w(f, "   Сознательно отдаём маржу: max скидка+буст → скорость → ранг → органика тянет весь профиль.")
        w(f, "   Делать ОГРАНИЧЕННО и временно (1-2 акции), это инвестиция в ранжирование, не норма.")
        tbl(f, velo, "be")
        w(f, "")
        w(f, f"🟢 УЧАСТВОВАТЬ С МАРЖОЙ — мин.цена = маржинальный пол (net ≥{TARGET_NET}₽), {len(margin)} SKU")
        w(f, "   WB даёт буст, но скидка ограничена нашим полом → маржа защищена, в 0 не уходим.")
        tbl(f, margin[:30], "fl")
        if len(margin) > 30:
            w(f, f"   … ещё {len(margin)-30} SKU в файле логики (показаны топ-30 по деньгам)")
        w(f, "")
        w(f, f"🔴 ИСКЛЮЧИТЬ ИЗ АКЦИИ — {len(excl)} SKU: себест душит (даже цена при 0 выше текущей)")
        w(f, "   Скидка увела бы в минус. Исключаем товар из авто-акции (мин.цена = текущая) или режем себест.")
        tbl(f, excl[:20], "cur")
        w(f, "")
        w(f, "ИТОГ: 🔥в0 " + str(len(velo)) + " | 🟢с маржой " + str(len(margin)) + " | 🔴исключить " + str(len(excl)))
        w(f, "Риск «снизили и не попали» закрыт: мин.цена — твёрдый пол, ниже него товар не продаётся.")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | 🔥в0 {len(velo)} | 🟢маржа {len(margin)} | 🔴исключить {len(excl)}")


if __name__ == "__main__":
    main()
