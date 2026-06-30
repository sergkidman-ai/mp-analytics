"""analyze_return_plan.py — план возврата «в плюс»: откат цены + реклама + срез расходов.

Юнит-экономика на РЕАЛЬНЫХ якорях из отчёта WB (margin_by_sku агрегат):
  NET(P) = P*(1 - комиссия% - возвраты%) - логистика_фикс - себест - реклама/заказ
Откат роняет цену → комиссия падает с ней, НО логистика фиксирована → маржа сжимается.
Значит «не в минус» = (а) рост объёма на breakeven-кратность, (б) срез себест/габаритов.

Источники: docs/rollback_digital.txt (цены, май-заказы), wb_cards (nm→vendor_code→external_code,
габариты), products (cost_seb мин. достижимая по external_code), wb_ad_nm (реклама/заказ), Джем.
→ docs/return_plan.txt
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "return_plan.txt"
ACC = "wb_acc1"
# якоря из реального отчёта WB
COMM = 0.132     # комиссия WB (% выручки покупателя)
RET = 0.025      # возвраты (% выручки)
LOGIST = 293     # логистика ₽/шт (почти фикс, не зависит от цены — ключевой рычаг)


def net_unit(price, cogs, ad=0):
    if price is None or cogs is None:
        return None
    return round(float(price) * (1 - COMM - RET) - LOGIST - float(cogs) - float(ad or 0))


def parse_rollback():
    """nmID -> (may_orders, cur_price, rollback_price, net_may_month)."""
    p = BASE_DIR / "docs" / "rollback_digital.txt"
    res = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        # nmID  май-зак  тек.цена  возврат  ...
        g = re.match(r"\s*(\d{6,})\s+(\d+)\s+(\d+)\s+(\d+)\b", line)
        if g:
            res[int(g.group(1))] = (int(g.group(2)), int(g.group(3)), int(g.group(4)), 0)
    return res


def w(f, s=""):
    f.write(s + "\n")


def main():
    roll = parse_rollback()
    nmids = list(roll)
    cards = {r["nm_id"]: r for r in db.query(
        "SELECT nm_id, vendor_code, volume_l, weight_kg, dims_valid "
        "FROM wb_cards WHERE account=%s AND nm_id=ANY(%s)", (ACC, nmids))}
    vc = {nm: c["vendor_code"] for nm, c in cards.items()}
    codes = list({v for v in vc.values()})
    prod = {r["external_code"]: r for r in db.query(
        "SELECT external_code, min(NULLIF(cost_seb,0)) seb, max(NULLIF(cost_seb,0)) seb_max, "
        "max(volume_l) vol FROM products WHERE external_code=ANY(%s) GROUP BY external_code", (codes,))}
    # СЕБЕСТ/шт — из витрины маржи по nm_id (решённый 3-слойный метод, покрытие ~полное), последний период.
    # НЕ через external_code->products.cost_seb (дырявый путь). Источник: reports/margin_by_sku.py.
    cogs_nm = {int(r["nm_id"]): float(r["seb"]) for r in db.query(
        "SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb "
        "FROM margin_by_sku WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 "
        "AND article ~ '^[0-9]+$' ORDER BY article, period_from DESC", (ACC,)) if r["seb"]}
    pos = {r["nm_id"]: r for r in db.query(
        "SELECT nm_id, avg_position, avg_position_dyn, orders FROM wb_search_report WHERE account=%s", (ACC,))}
    adn = {r["nm_id"]: r for r in db.query(
        "SELECT nm_id, sum(spend) sp, sum(orders) o, round(sum(spend)/NULLIF(sum(orders),0),0) ad_per_ord "
        "FROM wb_ad_nm WHERE account=%s GROUP BY nm_id", (ACC,))}

    greens, yellows, reds, nocogs = [], [], [], []
    for nm in nmids:
        may_o, cur, rb, net_may = roll[nm]
        code = vc.get(nm)
        pr = prod.get(code) or {}
        cogs = cogs_nm.get(nm)   # себест/шт из витрины маржи (nm_id)
        ad = (adn.get(nm) or {}).get("ad_per_ord") or 0
        card = cards.get(nm) or {}
        cvol = card["volume_l"] if card.get("dims_valid") else None   # реальный объём карточки WB
        rec = {"nm": nm, "code": code, "may_o": may_o, "cur": cur, "rb": rb, "cogs": cogs,
               "ad": ad, "vol": cvol, "seb_max": pr.get("seb_max"),
               "ncur": net_unit(cur, cogs, ad), "nrb": net_unit(rb, cogs, ad),
               "pos": (pos.get(nm) or {}).get("avg_position")}
        if cogs is None:
            nocogs.append(rec)
        elif rec["nrb"] is not None and rec["nrb"] >= 150:
            greens.append(rec)
        elif rec["nrb"] is not None and rec["nrb"] >= 0:
            yellows.append(rec)
        else:
            reds.append(rec)

    def tbl(f, recs):
        w(f, f"  {'nmID':>11} {'себест':>6} {'тек.ц':>6} {'откат':>6} {'net@тек':>8} {'net@откат':>10} "
             f"{'×объём':>7} {'рекл/зак':>8} {'поз':>4}")
        w(f, "  " + "-" * 88)
        for r in sorted(recs, key=lambda x: -(x["nrb"] or -9999)):
            mult = (r["ncur"] / r["nrb"]) if (r["ncur"] and r["nrb"] and r["nrb"] > 0) else None
            ms = f"{mult:.1f}x" if mult else "∞"
            w(f, f"  {r['nm']:>11} {r['cogs']:>6.0f} {r['cur']:>6} {r['rb']:>6} "
                 f"{str(r['ncur']):>8} {str(r['nrb']):>10} {ms:>7} {str(int(r['ad'])):>8} {str(r['pos'] or '—'):>4}")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "ПЛАН ВОЗВРАТА «В ПЛЮС» — ОТКАТ + РЕКЛАМА + СРЕЗ РАСХОДОВ (Цифровой)")
        w(f, "=" * 92)
        w(f, "0. РЕАЛЬНАЯ ЮНИТ-ЭКОНОМИКА WB (из отчёта реализации):")
        w(f, f"   комиссия {COMM*100:.1f}% | логистика {LOGIST}₽/шт (≈фикс!) | возвраты {RET*100:.1f}% | "
             f"себест ~37% | чистая ~32%")
        w(f, f"   NET(цена) = цена×{1-COMM-RET:.3f} − {LOGIST} − себест − реклама/заказ")
        w(f, "")
        w(f, "1. МЕХАНИКА «НЕ В МИНУС»:")
        w(f, "   • Откат роняет цену → комиссия падает ПРОПОРЦИОНАЛЬНО, а логистика 293₽ — НЕТ.")
        w(f, "     На дешёвой цене 293₽ съедают бОльшую долю → маржа сжимается сильнее цены.")
        w(f, "   • Колонка ×объём = во сколько раз надо поднять штуки, чтобы прибыль не упала после отката.")
        w(f, "     Джем-возврат позиции даёт этот объём; реклама (ДРР 3.6%) — катализатор почти даром.")
        w(f, "   • Рычаги, чтобы откат был в плюс: ↓себест (дешёвый поставщик) и ↓логистика (габарит).")
        w(f, "")
        w(f, f"2. 🟢 БЕЗОПАСНЫЙ ОТКАТ — net@откат ≥150₽/шт ({len(greens)} SKU): катим цену + рекламный импульс")
        tbl(f, greens)
        w(f, "")
        w(f, f"3. 🟡 ТОНКО — net@откат 0..150₽ ({len(yellows)} SKU): откат ТОЛЬКО со срезом себест/габарита")
        tbl(f, yellows)
        w(f, "")
        w(f, f"4. 🔴 В МИНУС при полном откате ({len(reds)} SKU): частичный откат или держать цену/реклама-стоп")
        tbl(f, reds)
        w(f, "")
        w(f, f"5. ❓ НЕТ СЕБЕСТОИМОСТИ — нельзя гарантировать плюс ({len(nocogs)} из {len(nmids)} SKU)")
        w(f, "   ПРЕДУСЛОВИЕ: дозаполнить cost_seb в МойСклад по этим external_code, потом считать откат.")
        w(f, "   " + ", ".join(sorted({r["code"] for r in nocogs if r["code"]}))[:300])
        w(f, "")
        w(f, "6. РЫЧАГИ СНИЖЕНИЯ РАСХОДОВ:")
        spread = [(r["code"], r["cogs"], r["seb_max"]) for r in (greens + yellows + reds)
                  if r.get("seb_max") and r["cogs"] and float(r["seb_max"]) > r["cogs"] * 1.5]
        w(f, f"   А) СЕБЕСТ — один external_code, разброс по поставщикам (брать дешёвый вариант):")
        for code, lo, hi in sorted(set(spread), key=lambda x: -(float(x[2]) - x[1]))[:12]:
            w(f, f"      {code}: дешёвый {lo:.0f}₽ vs дорогой {float(hi):.0f}₽  → экономия {float(hi)-lo:.0f}₽/шт")
        w(f, "   Б) ГАБАРИТ/ЛОГИСТИКА — логистика 293₽/шт (17%) почти не зависит от цены — главный фикс-расход.")
        w(f, "      ⚠ Данные объёма в каталоге НЕДОСТОВЕРНЫ (повторяется 11.1л-заглушка, встречается 34л на")
        w(f, "      картридж — невозможно). Поэтому габаритный рычаг нельзя считать по нашим данным.")
        w(f, "      ЧТО ДЕЛАТЬ: (1) свериться с реальной упаковкой по топ-объёмным позициям вручную;")
        w(f, "      (2) тариф WB растёт с литражом и коэффициентом склада — ужатие коробки на 0.5л экономит")
        w(f, "      десятки ₽/шт на фикс-293; (3) перед этим — почистить габариты карточек (отдельная задача).")
        w(f, "   В) РЕКЛАМА — общий ДРР 3.6%; импульс по позициям почти бесплатен, см. impulse_heroes.")
        w(f, "")
        w(f, "7. ПОРЯДОК ДЕЙСТВИЙ:")
        w(f, "   1) Дозаполнить себест (раздел 5) — иначе по 2/3 героев летим вслепую.")
        w(f, "   2) 🟢 катить цену сейчас + рекламный импульс (Canon Selphy, HP 1018 — см. импульс_герои).")
        w(f, "   3) 🟡 — сначала дешёвый поставщик / ужать короб, потом откат.")
        w(f, "   4) 🔴 — держать цену (уникальный/дорогой хвост) либо снять с рекламы, не лить впустую.")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | 🟢{len(greens)} 🟡{len(yellows)} 🔴{len(reds)} ❓{len(nocogs)}")


if __name__ == "__main__":
    main()
