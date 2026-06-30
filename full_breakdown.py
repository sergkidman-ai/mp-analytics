"""full_breakdown.py — раскладка по ВСЕМУ Джему (2097) + детальная экономика отката по 209 просевшим.

Цена возврата = майская реализованная цена (revenue_buyer/qty за 2026-05-01 из margin_by_sku) —
это даёт цель отката для ВСЕХ продававших, не только для списка 46. Себест/шт — из margin_by_sku
по nm_id (решённый источник). Юнит-экономика WB: net(P)=P*(1-комиссия-возвраты)-логистика-себест.
→ docs/full_breakdown.txt
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "full_breakdown.txt"
ACC = "wb_acc1"
COMM, RET, LOGIST = 0.132, 0.025, 293


def net_unit(price, cogs):
    if price is None or cogs is None:
        return None
    return round(float(price) * (1 - COMM - RET) - LOGIST - float(cogs))


def w(f, s=""):
    f.write(s + "\n")


def main():
    rows = db.query("""
        WITH cogs AS (
          SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb
          FROM margin_by_sku
          WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
          ORDER BY article, period_from DESC
        ),
        pmay AS (
          SELECT article::bigint nm_id, round(revenue_buyer/NULLIF(qty,0)) p_may
          FROM margin_by_sku
          WHERE platform='wb' AND account=%s AND period_from='2026-05-01' AND qty>0 AND article ~ '^[0-9]+$'
        )
        SELECT j.nm_id, j.name, j.pos, j.pos_dyn, j.open, j.open_dyn, j.price p_cur, j.is_advertised,
               f.order_count oc, f.order_sum os, cg.seb, pm.p_may
        FROM wb_jam_may j
        LEFT JOIN wb_funnel f ON f.account=j.account AND f.nm_id=j.nm_id AND f.period='2026-06-01'
        LEFT JOIN cogs cg ON cg.nm_id=j.nm_id
        LEFT JOIN pmay pm ON pm.nm_id=j.nm_id
        WHERE j.account=%s
    """, (ACC, ACC, ACC))

    def f0(x):
        return float(x or 0)

    total = len(rows)
    sellers = [r for r in rows if (r["oc"] or 0) > 0]
    tail = [r for r in rows if (r["oc"] or 0) == 0 and ((r["open"] or 0) > 0 or (r["open_dyn"] or 0) < 0)]
    dead = [r for r in rows if (r["oc"] or 0) == 0 and (r["open"] or 0) == 0 and (r["open_dyn"] or 0) >= 0]

    def dropped(r):
        return (r["pos_dyn"] or 0) > 0 or (r["open_dyn"] or 0) < 0
    sell_drop = [r for r in sellers if dropped(r)]

    # экономика по каждому: net@тек, net@май, признак "поднимали цену"
    for r in rows:
        r["p_cur"] = float(r["p_cur"]) if r["p_cur"] is not None else None
        r["p_may"] = float(r["p_may"]) if r["p_may"] is not None else None
        r["seb"] = float(r["seb"]) if r["seb"] is not None else None
        r["net_cur"] = net_unit(r["p_cur"], r["seb"])
        r["net_may"] = net_unit(r["p_may"], r["seb"])
        r["up"] = bool(r["p_cur"] and r["p_may"] and r["p_cur"] > r["p_may"] * 1.03)

    have_cogs = [r for r in sellers if r["seb"]]
    prof_cur = [r for r in have_cogs if r["net_cur"] and r["net_cur"] > 0]

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "ПОЛНАЯ РАСКЛАДКА ПО ДЖЕМУ (2097) + ЭКОНОМИКА ОТКАТА ПО 209 ПРОСЕВШИМ")
        w(f, f"net(P)=P×{1-COMM-RET:.3f}−{LOGIST}−себест | цена возврата=майская реализ. (rev/qty) | себест из margin_by_sku")
        w(f, "=" * 104)

        w(f, "\n1. СЕГМЕНТЫ ВСЕГО КАТАЛОГА")
        srev = sum(f0(r["os"]) for r in sellers)
        w(f, f"   🟢 ПРОДАЮТ:                 {len(sellers):>4} SKU | выручка июнь {srev:,.0f} ₽ | себест есть у {len(have_cogs)}")
        w(f, f"       из них прибыльны@тек.цене: {len(prof_cur):>3} | просели (поз/показы): {len(sell_drop)}")
        w(f, f"   🟡 ХВОСТ (был/есть трафик):  {len(tail):>4} SKU | сейчас не продают; возврат — массово видимостью")
        w(f, f"   ⚪ СПЯЩИЕ (нет трафика):      {len(dead):>4} SKU | контент/перезапуск")

        # экономика отката агрегатом по 209
        sd_cogs = [r for r in sell_drop if r["seb"] and r["p_may"]]
        roll_ok = [r for r in sd_cogs if r["net_may"] and r["net_may"] >= 150]
        roll_thin = [r for r in sd_cogs if r["net_may"] is not None and 0 <= r["net_may"] < 150]
        roll_neg = [r for r in sd_cogs if r["net_may"] is not None and r["net_may"] < 0]
        no_may = [r for r in sell_drop if not r["p_may"]]
        risk = sum(f0(r["os"]) for r in sell_drop)
        w(f, "\n2. ЭКОНОМИКА ОТКАТА ПО 209 ПРОСЕВШИМ ПРОДАЮЩИМ (цена → майская)")
        w(f, f"   Под риском выручки: {risk:,.0f} ₽/мес. С майской ценой и известным себестом:")
        w(f, f"   🟢 откат безопасен (net@май ≥150₽):  {len(roll_ok):>3} SKU")
        w(f, f"   🟡 тонко (net@май 0..150₽):           {len(roll_thin):>3} SKU — нужен дешёвый поставщик")
        w(f, f"   🔴 в минус при майской цене:           {len(roll_neg):>3} SKU — держать цену/не откатывать полностью")
        w(f, f"   ❓ не продавали в мае (нет цены возврата): {len(no_may):>3} SKU — откат к произвольной цене, считать вручную")

        w(f, "\n3. ДЕТАЛЬНО — 209 ПРОСЕВШИХ ПРОДАЮЩИХ (сорт по выручке июня)")
        w(f, f"  {'nmID':>11} {'выр.июнь':>9} {'зак':>3} {'поз':>4} {'Δпоз':>6} {'тек.ц':>6} {'май.ц':>6} "
             f"{'себ':>5} {'net@тек':>8} {'net@май':>8} {'×об':>5} {'цена↑':>6}  товар")
        w(f, "  " + "-" * 116)
        for r in sorted(sell_drop, key=lambda r: -f0(r["os"])):
            pd = r["pos_dyn"]
            pds = ("▲+%d" % pd) if (pd or 0) > 0 else ("▼%d" % pd if pd else "0")
            mult = (r["net_cur"] / r["net_may"]) if (r["net_cur"] and r["net_may"] and r["net_may"] > 0) else None
            ms = f"{mult:.1f}x" if mult else ("∞" if r["net_may"] is not None and r["net_may"] <= 0 else "—")
            up = ""
            if r["p_cur"] and r["p_may"]:
                up = f"+{round((r['p_cur']/r['p_may']-1)*100)}%"
            w(f, f"  {r['nm_id']:>11} {f0(r['os']):>9,.0f} {r['oc'] or 0:>3} {str(r['pos'] or '—'):>4} {pds:>6} "
                 f"{str(r['p_cur'] or '—'):>6} {str(r['p_may'] or '—'):>6} {str(int(r['seb'])) if r['seb'] else '—':>5} "
                 f"{str(r['net_cur']) if r['net_cur'] is not None else '—':>8} "
                 f"{str(r['net_may']) if r['net_may'] is not None else '—':>8} {ms:>5} {up:>6}  {(r['name'] or '')[:24]}")
        w(f, "\nЛегенда: net@тек/net@май — чистая ₽/шт при текущей/майской цене; ×об — во сколько раз нужно")
        w(f, "поднять объём, чтобы прибыль не упала после отката (даёт возврат позиции+реклама); цена↑ — на сколько подняли с мая.")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | продают {len(sellers)} (просели {len(sell_drop)}, себест есть {len(have_cogs)}) | "
          f"откат 🟢{len(roll_ok)} 🟡{len(roll_thin)} 🔴{len(roll_neg)} ❓{len(no_may)}")


if __name__ == "__main__":
    main()
