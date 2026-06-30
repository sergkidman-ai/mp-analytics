"""global_plan.py — ГЛОБАЛЬНЫЙ план: два двигателя (ядро продаж + длинный хвост-опциональность).

Хвост — не мёртвый груз: каждая карточка в выдаче = шанс на редкую продажу; на большом ассортименте
сумма редких продаж реальна. Выпали из выдачи → потеряли сам шанс. Поэтому план = (A) вернуть деньги
по ядру (откат+реклама, экономика), (B) вернуть ВИДИМОСТЬ хвоста массово (тот же корневой фикс цены +
дешёвый рекламный посев), мерим охватом/показами, а не юнит-экономикой каждой карточки.

Источники: wb_jam_may (вся выдача vs май), wb_funnel (продажи июнь), products (себест), wb_ad_nm, откат-лист.
→ docs/global_plan.txt
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "global_plan.txt"
ACC = "wb_acc1"


def rollback_nmids():
    p = BASE_DIR / "docs" / "rollback_digital.txt"
    s = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            g = re.match(r"\s*(\d{6,})\s", line)
            if g:
                s.add(int(g.group(1)))
    return s


def w(f, s=""):
    f.write(s + "\n")


def main():
    roll = rollback_nmids()
    rows = db.query("""
        WITH cogs AS (
          -- себест/шт из витрины маржи (ключ = nm_id, покрытие ~полное, 3 слоя), последний период
          SELECT DISTINCT ON (article) article::bigint nm_id, round(cogs/NULLIF(qty,0)) seb
          FROM margin_by_sku
          WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
          ORDER BY article, period_from DESC
        )
        SELECT j.nm_id, j.name, j.pos, j.pos_dyn, j.open, j.open_dyn, j.vis_dyn,
               j.price, j.is_advertised,
               f.order_count oc, f.order_sum os,
               cg.seb, a.spend ad_spend
        FROM wb_jam_may j
        LEFT JOIN wb_funnel f ON f.account=j.account AND f.nm_id=j.nm_id AND f.period='2026-06-01'
        LEFT JOIN cogs cg ON cg.nm_id=j.nm_id
        LEFT JOIN (SELECT nm_id, sum(spend) spend FROM wb_ad_nm WHERE account=%s GROUP BY nm_id) a ON a.nm_id=j.nm_id
        WHERE j.account=%s
    """, (ACC, ACC, ACC))

    def f0(x):
        return float(x or 0)

    total = len(rows)
    sellers = [r for r in rows if (r["oc"] or 0) > 0]
    sell_rev = sum(f0(r["os"]) for r in sellers)
    # хвост-опциональность: не продаёт сейчас, но имел/имеет видимость (был трафик)
    tail_opt = [r for r in rows if (r["oc"] or 0) == 0 and ((r["open"] or 0) > 0 or (r["open_dyn"] or 0) < 0)]
    dead = [r for r in rows if (r["oc"] or 0) == 0 and (r["open"] or 0) == 0 and (r["open_dyn"] or 0) >= 0]

    def dropped(r):
        return (r["pos_dyn"] or 0) > 0 or (r["open_dyn"] or 0) < 0
    fell_out = [r for r in rows if r["pos"] == 0 and (r["pos_dyn"] or 0) > 0]

    sell_drop = [r for r in sellers if dropped(r)]
    sell_drop_rev = sum(f0(r["os"]) for r in sell_drop)
    tail_fell = [r for r in tail_opt if r["pos"] == 0]

    # вклад «хвостовых» продаж в выручку июня (билеты лотереи реальны)
    tiers = {"1 заказ": (1, 1), "2-3": (2, 3), "4-9": (4, 9), "10+": (10, 10**9)}
    tier_rev, tier_n = {}, {}
    for label, (lo, hi) in tiers.items():
        grp = [r for r in sellers if lo <= (r["oc"] or 0) <= hi]
        tier_rev[label] = sum(f0(r["os"]) for r in grp)
        tier_n[label] = len(grp)
    tail_sales_rev = tier_rev["1 заказ"] + tier_rev["2-3"]
    tail_sales_n = tier_n["1 заказ"] + tier_n["2-3"]

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "ГЛОБАЛЬНЫЙ ПЛАН ВОЗВРАТА — ДВА ДВИГАТЕЛЯ (ядро продаж + длинный хвост)")
        w(f, "Джем (просадка vs май) × выручка (июнь) × себест × реклама. Хвост = опциональность, не мусор.")
        w(f, "=" * 100)

        w(f, "\n1. ИЗ ЧЕГО СОСТОИТ КАТАЛОГ (Джем, " + str(total) + " карточек)")
        w(f, f"   🟢 ЯДРО — продают сейчас:                {len(sellers):>4} SKU | выручка {sell_rev:,.0f} ₽/мес")
        w(f, f"   🟡 ХВОСТ-ОПЦИОНАЛЬНОСТЬ — есть/был трафик,  {len(tail_opt):>4} SKU | сейчас не продают, но видимы/были видимы")
        w(f, f"      из них ВЫПАЛИ из выдачи (поз 0):       {len(tail_fell):>4} SKU ← потеряли «лотерейные билеты»")
        w(f, f"   ⚪ СПЯЩИЕ — ни продаж, ни трафика:         {len(dead):>4} SKU | контент/перезапуск, низкий приоритет")

        w(f, "\n2. ХВОСТ РЕАЛЬНО ДАЁТ ДЕНЬГИ (вклад редких продаж в июне)")
        for label in ("1 заказ", "2-3", "4-9", "10+"):
            w(f, f"   {label:>8}: {tier_n[label]:>4} SKU | {tier_rev[label]:>11,.0f} ₽ ({round(tier_rev[label]/sell_rev*100) if sell_rev else 0}%)")
        w(f, f"   → «хвостовые» продажи (1–3 заказа): {tail_sales_n} SKU дают {tail_sales_rev:,.0f} ₽/мес.")
        w(f, f"     Это доказывает: широкая выдача = деньги. Вернув видимость 845 выпавших, расширяем этот пул.")

        w(f, "\n3. ДВИГАТЕЛЬ A — ЯДРО (вернуть деньги, экономика по SKU)")
        w(f, f"   Просели и продают: {len(sell_drop)} SKU | под риском {sell_drop_rev:,.0f} ₽/мес ({round(sell_drop_rev/sell_rev*100) if sell_rev else 0}% выручки ядра).")
        w(f, "   Действие: откат цены + рекламный импульс там, где есть себест и плюс (см. план_возврата, импульс_герои).")
        w(f, f"  {'nmID':>11} {'выр.июнь':>9} {'зак':>4} {'поз':>4} {'Δпоз':>6} {'Δпок%':>6} {'цена':>6} {'себ':>5} {'откат':>5}  товар")
        w(f, "  " + "-" * 100)
        for r in sorted(sell_drop, key=lambda r: -f0(r["os"]))[:35]:
            pd = r["pos_dyn"]; od = r["open_dyn"]
            pds = ("▲+%d" % pd) if (pd or 0) > 0 else ("▼%d" % pd if pd else "0")
            ods = (str(od) + "%") if od is not None else "—"
            seb_s = f"{float(r['seb']):.0f}" if r["seb"] else "—"
            rl = "ДА" if r["nm_id"] in roll else "—"
            nm_name = (r["name"] or "")[:28]
            w(f, f"  {r['nm_id']:>11} {f0(r['os']):>9,.0f} {r['oc'] or 0:>4} {str(r['pos'] or '—'):>4} {pds:>6} "
                 f"{ods:>6} {str(r['price'] or '—'):>6} {seb_s:>5} {rl:>5}  {nm_name}")

        w(f, "\n4. ДВИГАТЕЛЬ B — ХВОСТ (вернуть ВИДИМОСТЬ массово, не юнит-экономикой)")
        w(f, f"   {len(tail_fell)} выпавших карточек с (бывшим) трафиком — кандидаты на возврат в выдачу.")
        w(f, "   Корневая причина та же: сквозной рост цены уронил конверсию → WB снял весь каталог с ранжирования,")
        w(f, "   хвост (самый чувствительный к цене и низкоскоростной) вылетел первым.")
        w(f, "   РЫЧАГИ массовые (не по одной карточке):")
        w(f, "     • откатить сквозное повышение цены по хвосту (вернёт органическое ранжирование — бесплатно);")
        w(f, "     • дешёвый рекламный посев минимальной ставкой (ДРР 3.6%) — поднять скорость → ранг → органика;")
        w(f, "     • гигиена контента/ключей; следить за стоком (нет остатка = нет выдачи).")
        w(f, "   Метрика успеха: показы карточек и «в топ-100» (сейчас −105 и −18%), число продающих SKU, а не маржа каждой.")

        w(f, "\n5. ИТОГ ПЛАНА")
        w(f, f"   • Ядро: вернуть {sell_drop_rev:,.0f} ₽/мес — точечно, с экономикой (предусловие: дозаполнить себест).")
        w(f, f"   • Хвост: вернуть в выдачу {len(tail_fell)} карточек — массово, ценой+посевом; цель — расширить пул редких продаж ({tail_sales_rev:,.0f} ₽ уже даёт).")
        w(f, f"   • Спящие {len(dead)} — отдельный трек контента/перезапуска, не сейчас.")
        w(f, "\n— конец —")
    print(f"Готово: {OUT} | ядро {len(sellers)} (просели {len(sell_drop)}, риск {sell_drop_rev:,.0f}₽) | "
          f"хвост-опц {len(tail_opt)} (выпали {len(tail_fell)}) | спящие {len(dead)}")


if __name__ == "__main__":
    main()
