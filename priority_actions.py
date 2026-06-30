"""priority_actions.py — приоритезированный план действий по 209 просевшим: что в первую очередь.

Для каждого SKU считаем РЕКОМЕНДОВАННУЮ цену частичного отката = минимальную цену, при которой
чистая ≥ цели (TARGET_NET ₽/шт). Не ниже майской (она работала) и не выше текущей. Делим на волны
по деньгам. Себест из margin_by_sku (nm_id), юнит-экономика WB. → docs/priority_actions.txt
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "priority_actions.txt"
ACC = "wb_acc1"
COMM, RET, LOGIST = 0.132, 0.025, 293
K = 1 - COMM - RET            # 0.843
TARGET_NET = 400             # целевая чистая ₽/шт после отката (запас прочности)


def net_unit(price, cogs):
    if price is None or cogs is None:
        return None
    return round(float(price) * K - LOGIST - float(cogs))


def price_for_net(target, cogs):
    return (target + LOGIST + float(cogs)) / K


def w(f, s=""):
    f.write(s + "\n")


def main():
    rows = db.query("""
        WITH cogs AS (
          SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb
          FROM margin_by_sku WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
          ORDER BY article, period_from DESC),
        pmay AS (
          SELECT article::bigint nm_id, round(revenue_buyer/NULLIF(qty,0)) p_may
          FROM margin_by_sku WHERE platform='wb' AND account=%s AND period_from='2026-05-01'
            AND qty>0 AND article ~ '^[0-9]+$')
        SELECT j.nm_id, j.name, j.pos, j.pos_dyn, j.open_dyn, j.price p_cur, j.is_advertised adv,
               f.order_count oc, f.order_sum os, cg.seb, pm.p_may
        FROM wb_jam_may j
        JOIN wb_funnel f ON f.account=j.account AND f.nm_id=j.nm_id AND f.period='2026-06-01' AND f.order_count>0
        LEFT JOIN cogs cg ON cg.nm_id=j.nm_id
        LEFT JOIN pmay pm ON pm.nm_id=j.nm_id
        WHERE j.account=%s AND (j.pos_dyn>0 OR j.open_dyn<0)
    """, (ACC, ACC, ACC))

    for r in rows:
        for k in ("p_cur", "p_may", "seb"):
            r[k] = float(r[k]) if r[k] is not None else None

    # Целевая цена = ПРОХОДНАЯ-ПРОКСИ (майская реализованная — тогда товар был в акции/выдаче).
    # net@акция = чистая при этой цене. Маржинальный пол (для net≥TARGET) — индикатор «безопасно/тонко».
    A, B, C, Q = [], [], [], []   # катить к акции безопасно / тонко-режем себест / минус-держим / нет майцены
    for r in rows:
        pc, pm, sb = r["p_cur"], r["p_may"], r["seb"]
        if sb is None or pc is None or pm is None:
            Q.append(r); r["rec"] = None; continue
        r["rec"] = pm                                   # цель = проходная (майская)
        r["net_rec"] = net_unit(pm, sb)                 # чистая в акции
        r["floor"] = round(price_for_net(TARGET_NET, sb))  # пол под целевую маржу (справочно)
        r["room_pct"] = round((pc - pm) / pc * 100) if pc else 0
        nm = r["net_rec"]
        if nm is not None and nm >= 150:
            A.append(r)                                 # в акции маржа здоровая → катить
        elif nm is not None and nm >= 0:
            B.append(r)                                 # в акции маржа тонкая → срезать себест
        else:
            C.append(r)                                 # в акции минус → держать цену/не входить

    def f0(x):
        return float(x or 0)
    by_rev = lambda lst: sorted(lst, key=lambda r: -f0(r["os"]))

    def block(f, lst, title, note):
        rv = sum(f0(r["os"]) for r in lst)
        w(f, f"\n{title}  —  {len(lst)} SKU, выручка под этим {rv:,.0f} ₽/мес")
        w(f, f"  {note}")
        w(f, f"  {'nmID':>11} {'выр.июнь':>9} {'поз':>4} {'тек.ц':>6} {'→акция':>7} {'скидка':>7} {'net@акц':>7} {'рекл':>5}  товар")
        w(f, "  " + "-" * 96)
        for r in by_rev(lst):
            rec = str(int(r["rec"])) if r.get("rec") else "—"
            room = f"-{r['room_pct']}%" if r.get("room_pct") else "—"
            net = str(r.get("net_rec")) if r.get("net_rec") is not None else "—"
            adv = "да" if r["adv"] else "—"
            w(f, f"  {r['nm_id']:>11} {f0(r['os']):>9,.0f} {str(r['pos'] or '—'):>4} "
                 f"{str(int(r['p_cur'])) if r['p_cur'] else '—':>6} {rec:>7} {room:>7} {net:>7} {adv:>5}  {(r['name'] or '')[:26]}")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "ПЛАН ВОЗВРАТА «В ИГРУ» — ПО ПРИОРИТЕТАМ (Цифровой, 209 просевших продающих)")
        w(f, "=" * 100)
        w(f, "КАК ВОЗВРАЩАЕМСЯ — УМНО, БЕЗ МИНУСОВ (короткая заметка):")
        w(f, "  • →акция = майская реализ. цена (ДОКАЗАННАЯ проходная: тогда товар был в акции и продавался).")
        w(f, "    net@акц = чистая ₽/шт при этой цене. Проходную авто-акции WB по API не отдаёт — май это прокси.")
        w(f, "  • ГЛАВНЫЙ РИСК (учтён): можно снизить цену и НЕ попасть в акцию → продажи не вернутся,")
        w(f, "    а маржа уже ниже. Поэтому НЕ катим весь список вслепую.")
        w(f, "  • ПОРЯДОК БЕЗ МИНУСА: (1) сначала РЕКЛАМНЫЙ импульс при ТЕКУЩЕЙ цене — скорость поднимает ранг")
        w(f, "    и без акции, и это обратимо/дёшево (ДРР 3.6%). (2) КАНАРЕЙКА: на 2-3 топ-SKU роняем цену к →акция,")
        w(f, "    7 дней смотрим заказы(daily)+позицию(Джем). Вернулся объём → катим остальных. Нет → откат назад.")
        w(f, "  • Где net@акц тонкий/минус — сперва срезать себест или держать цену. Хвост — массово видимостью.")

        block(f, A, "🟢 ВОЛНА 1 — КАНДИДАТЫ НА ОТКАТ К АКЦИИ (в акции маржа здоровая, net@акц ≥150)",
              "Сначала канарейка на топ-2-3 по деньгам: цена→акция + ставка, замер 7 дней. Подтвердилось — катим вниз по списку.")
        block(f, B, "🟡 ВОЛНА 2 — В АКЦИИ МАРЖА ТОНКАЯ (net@акц 0..150) — СНАЧАЛА ДЕШЁВЫЙ ПОСТАВЩИК",
              "Откат к акции даёт тонкую маржу — сперва снизить себест (тот же external_code дешевле), тогда плюс.")
        block(f, C, "🔴 ВОЛНА 3 — В АКЦИИ МИНУС — ДЕРЖАТЬ ЦЕНУ",
              "На проходной цене уходим в минус. Не входить в акцию ценой — держать цену, продавать одиночно дороже.")
        if Q:
            block(f, Q, "❓ ВОЛНА 4 — НЕТ МАЙСКОЙ ЦЕНЫ (считать вручную)",
                  "В мае не продавались — нет якоря цены возврата. Назначить цену вручную по конкурентам.")

        w(f, "\n" + "=" * 100)
        w(f, "ИТОГ ПО ПРИОРИТЕТАМ:")
        w(f, f"  ВОЛНА 1 (катить сейчас):     {len(A):>3} SKU | {sum(f0(r['os']) for r in A):,.0f} ₽/мес — основной возврат")
        w(f, f"  ВОЛНА 2 (себест, потом откат):{len(B):>3} SKU | {sum(f0(r['os']) for r in B):,.0f} ₽/мес")
        w(f, f"  ВОЛНА 3 (держать цену):       {len(C):>3} SKU | {sum(f0(r['os']) for r in C):,.0f} ₽/мес")
        w(f, f"  ВОЛНА 4 (вручную):            {len(Q):>3} SKU | {sum(f0(r['os']) for r in Q):,.0f} ₽/мес")
        w(f, "  + ХВОСТ: 761 выпавшая карточка — массово: общий частичный откат по хвосту + дешёвый посев,")
        w(f, "    метрика — показы/топ-100, а не маржа каждой (см. global_plan.txt).")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | Волна1 {len(A)} ({sum(f0(r['os']) for r in A):,.0f}₽) | "
          f"Волна2 {len(B)} | Волна3 {len(C)} | Волна4 {len(Q)}")


if __name__ == "__main__":
    main()
