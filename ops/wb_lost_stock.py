#!/usr/bin/env python3
# поток: ev — оценка товара, утраченного на пострадавших складах ВБ.
# Остаток берём из НАШИХ снимков wb_stocks на дату удара (или ближайший до неё).
#
# Себест/шт — ЦЕНА ПРИЁМКИ МС, ближайшая к дате удара и НЕ ПОЗЖЕ неё (метод Натальи 21.08).
# Почему не FIFO отгрузок: FIFO отвечает «по чём списали при ПРОДАЖЕ», а этот товар не продан —
# он лежал на складе МП, номера отгрузки, которой он туда попал (отмена/невыкуп), у нас нет,
# даты поступления на склад ВБ — тоже. Значит берём последнюю цену закупки до даты удара.
# Источник — ms_supply_pos (ops/ms_supply_prices.py).
# Витрина mkt_sku_economics считается ВТОРЫМ методом, только для сверки: у неё половина SKU
# оценена сегодняшним прайсом поставщика (cogs_source='live'), а не нашей закупкой.
import sys, datetime as dt
from collections import Counter
from core import db
from reports.margin_control import _mapping   # read-only: общий мост nm_id -> код МС

# склад в wb_stocks -> (имя из новостей, дата удара, статус)
TARGETS = [
    ("Электросталь",           "Электросталь",         "2026-07-18", "lost"),
    ("Котовск",                "Котовск",              "2026-07-18", "lost"),
    ("Владимир",               "Владимир: Воршинское", "2026-07-25", "lost"),
    ("Самара (Новосемейкино)", "Новосемейкино",        "2026-08-02", "lost"),
    ("Тула",                   "Алексин (склад ВБ «Тула»)", "2026-08-04", "lost"),
]
# серьёзно повреждены — товар под риском, ВБ обещал показать пострадавший остаток отдельной
# колонкой в «Отчёте по остаткам» (новость 16.08). Считаем ту же корзину, но это НЕ утрата.
DAMAGED = [
    ("Краснодар",    "Краснодар",                "2026-07-22", "damaged"),
    ("Невинномысск", "Невинномысск",             "2026-07-22", "damaged"),
    ("СПБ Шушары",   "Шушары (склад)",           "2026-07-24", "damaged"),
    ("СЦ Шушары",    "Шушары (сортировочный центр)", "2026-07-24", "damaged"),
    ("Коледино",     "Коледино (Подольск)",      "2026-08-16", "damaged"),
]
ABSENT = [("Чехов / Новосёлки", "2026-08-16"), ("Северное Домодедово", "2026-08-16")]


def nm_composition():
    """nm_id -> [(код карточки МС, штук на единицу WB), ...].

    Половина карточек WB — НАБОРЫ («Картриджи DS TN-514» = 4 картриджа CMYK), и мост
    `_mapping` отдаёт только один компонент (самый частый) — по нему набор оценивается
    вчетверо дешевле. Поэтому состав берём из ФАКТИЧЕСКИХ отгрузок МС: отправление WB
    (`assembly_id`) → документ отгрузки → его позиции; берём самый частый состав.
    Карточки без отгрузок — фолбэк на общий мост, один товар, 1 шт."""
    from collections import Counter, defaultdict
    pos = defaultdict(list)
    for r in db.query("""SELECT ps.demand_id, p.external_code ec, ps.qty
                         FROM ms_demand_pos ps JOIN ms_product p ON p.ms_id = ps.ms_id
                         WHERE p.external_code IS NOT NULL"""):
        pos[r["demand_id"]].append((r["ec"], float(r["qty"])))
    variants = defaultdict(Counter)
    for acc in ("wb_acc1", "wb_acc2"):
        for r in db.query("""SELECT DISTINCT w.payload->>'nm_id' nm, d.demand_id
                             FROM raw_wb_report w
                             JOIN ms_demand_cogs d ON d.demand_name = w.payload->>'assembly_id'
                             WHERE w.account=%s AND w.payload->>'nm_id' ~ '^[0-9]+$'""", (acc,)):
            if r["demand_id"] in pos:
                variants[int(r["nm"])][tuple(sorted(pos[r["demand_id"]]))] += 1
    comp = {nm: list(c.most_common(1)[0][0]) for nm, c in variants.items()}
    codes = {r["external_code"] for r in db.query(
        "SELECT DISTINCT external_code FROM ms_product WHERE external_code IS NOT NULL")}
    for acc in ("wb_acc1", "wb_acc2"):
        for nm, (ec, _src) in _mapping(acc, codes).items():
            comp.setdefault(int(nm), [(ec, 1.0)])
    return comp


def supply_cost(hit, comp):
    """nm_id -> (себест единицы WB, дата приёмки, число компонентов).
    Цена компонента — последняя приёмка МС НЕ ПОЗЖЕ даты удара. Под карточкой несколько
    товаров-поставщиков (`3804at`, `3804wb` при коде `3804`) — берём последнюю приёмку любого.
    Набор, у которого хоть один компонент без приёмки, в сумму НЕ идёт: цифра для претензии
    должна быть целой."""
    last = {}
    for r in db.query("""SELECT DISTINCT ON (p.external_code)
                                p.external_code ec, s.price_rub, s.moment
                         FROM ms_supply_pos s JOIN ms_product p ON p.ms_id = s.ms_id
                         WHERE s.moment < %s::date + 1 AND s.price_rub > 0
                         ORDER BY p.external_code, s.moment DESC""", (hit,)):
        last[r["ec"]] = (float(r["price_rub"]), r["moment"].date())
    out = {}
    for nm, parts in comp.items():
        if not parts or any(ec not in last for ec, _q in parts):
            continue
        out[nm] = (sum(last[ec][0] * q for ec, q in parts),
                   max(last[ec][1] for ec, _q in parts), len(parts))
    return out

def vitrina_cost():
    """Сверочный метод: nm_id -> (себест/шт, источник) из витрины mkt_sku_economics."""
    return {int(r["nm_id"]): (float(r["cogs_u"]), r["cogs_source"]) for r in db.query(
        """SELECT DISTINCT ON (nm_id) nm_id, cogs_u, cogs_source
           FROM mkt_sku_economics WHERE cogs_u > 0 ORDER BY nm_id, built_at DESC""")}


def bad_days():
    """Дни с обрезанным снимком: строк заметно меньше, чем у соседних дней (окно +-3).
    Объём выгрузки ВБ менялся скачками (смена эндпоинта 20.07) — сравнение только локальное."""
    rows = db.query("SELECT captured_at::date d, count(*) n FROM wb_stocks GROUP BY 1 ORDER BY 1")
    bad = set()
    for i, r in enumerate(rows):
        near = sorted(x["n"] for x in rows[max(0, i - 3):i + 4] if x is not r)
        if near and r["n"] < 0.7 * near[len(near) // 2]:
            bad.add(r["d"])
    return bad


def snapshot_date(wh, hit, bad):
    """Дата снимка: сам день удара, иначе ближайший ДО него, иначе ближайший после."""
    for order, sign in (("DESC", "<="), ("ASC", ">")):
        for r in db.query(f"""SELECT captured_at::date d FROM wb_stocks
                              WHERE warehouse=%s AND captured_at::date {sign} %s::date
                              ORDER BY captured_at {order} LIMIT 10""", (wh, hit)):
            if r["d"] not in bad:
                return r["d"], ("день удара" if str(r["d"]) == hit else
                                ("ближайший до" if sign == "<=" else "ближайший после"))
    return None, None


def rub(x):
    return f"{x:,.0f}".replace(",", " ")


def wh_block(wh, label, hit, status, bad, comp, vit, out, store):
    """Одна секция отчёта. Возвращает (штук, сумма по приёмке, штук без цены, сумма по витрине)."""
    d, how = snapshot_date(wh, hit, bad)
    if not d:
        out.append(f"### {label}\nСнимков остатков по этому складу нет.\n")
        return 0, 0.0, 0, 0.0
    sup = supply_cost(hit, comp)
    rows = db.query("""SELECT account, nm_id, vendor_code, sum(quantity) q
                       FROM wb_stocks WHERE warehouse=%s AND captured_at::date=%s
                       GROUP BY 1,2,3 HAVING sum(quantity)>0 ORDER BY 4 DESC""", (wh, d))
    q = sum(int(r["q"]) for r in rows)
    priced = [(r, sup[int(r["nm_id"])]) for r in rows if int(r["nm_id"]) in sup]
    s = sum(int(r["q"]) * c[0] for r, c in priced)
    nc = q - sum(int(r["q"]) for r, _ in priced)
    v = sum(int(r["q"]) * vit[int(r["nm_id"])][0] for r in rows if int(r["nm_id"]) in vit)
    lag = (dt.date.fromisoformat(hit) - d).days
    out.append(f"### {label} — удар {dt.date.fromisoformat(hit):%d.%m.%Y}")
    out.append(f"Снимок {d:%d.%m.%Y} ({how}" + (f", разрыв {abs(lag)} дн." if lag else "") + ")")
    out.append(f"Штук {q}, SKU {len(rows)}, по цене приёмки **{rub(s)} ₽**"
               + (f", без приёмки в базе {nc} шт" if nc else ""))
    out.append(f"Для сверки, по витрине `mkt_sku_economics`: {rub(v)} ₽")
    after = [a for a in db.query("""SELECT captured_at::date d, sum(quantity) q FROM wb_stocks
                                    WHERE warehouse=%s AND captured_at::date > %s::date
                                    GROUP BY 1 ORDER BY 1""", (wh, hit)) if a["d"] not in bad]
    if after:
        vals = [int(a["q"]) for a in after]
        move = sum(abs(vals[i] - vals[i-1]) for i in range(1, len(vals)))
        out.append(f"\nКонтроль по снимкам после удара: {after[0]['d']:%d.%m} — {vals[0]} шт, "
                   f"последний снимок {after[-1]['d']:%d.%m} — {vals[-1]} шт; движение за "
                   f"{len(after)} дн. {move} шт "
                   + ("(остаток фактически заморожен — склад не отгружает)." if move <= 2
                      else "(остаток шевелится — товар живой)."))
        gone = db.query("SELECT max(captured_at)::date d FROM wb_stocks WHERE warehouse=%s", (wh,))[0]["d"]
        last = db.query("SELECT max(captured_at)::date d FROM wb_stocks")[0]["d"]
        if gone < last:
            out.append(f"С {gone:%d.%m.%Y} склад из отчёта ВБ пропал — в свежих снимках его нет "
                       f"(последний снимок базы {last:%d.%m.%Y}).")
    out.append("\n| Аккаунт | nmID | Артикул | Шт | В карточке | Цена приёмки | Дата приёмки | Сумма | Витрина, ₽/шт |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r, c in sorted(priced, key=lambda x: -int(x[0]["q"]) * x[1][0]):
        vv = vit.get(int(r["nm_id"]))
        out.append(f"| {r['account']} | {r['nm_id']} | {r['vendor_code']} | {int(r['q'])} | {c[2]} шт | "
                   f"{rub(c[0])} | {c[1]:%d.%m.%Y} | {rub(int(r['q'])*c[0])} | "
                   + (f"{rub(vv[0])} ({vv[1]}) |" if vv else "— |"))
    for r in rows:
        if int(r["nm_id"]) not in sup:
            vv = vit.get(int(r["nm_id"]))
            out.append(f"| {r['account']} | {r['nm_id']} | {r['vendor_code']} | {int(r['q'])} | "
                       f"{len(comp.get(int(r['nm_id']),[]))} шт | — | приёмки нет | — | "
                       + (f"{rub(vv[0])} ({vv[1]}) |" if vv else "— |"))
    out.append("")
    for r in rows:
        nm = int(r["nm_id"]); c = sup.get(nm); vv = vit.get(nm)
        store.append({"warehouse": wh, "wh_label": label, "status": status, "hit_date": hit,
                      "snap_date": d, "snap_how": how, "account": r["account"], "nm_id": nm,
                      "vendor_code": r["vendor_code"], "qty": int(r["q"]),
                      "components": c[2] if c else len(comp.get(nm, [])) or None,
                      "unit_cost": c[0] if c else None, "supply_date": c[1] if c else None,
                      "cost_total": int(r["q"]) * c[0] if c else None,
                      "vitrina_u": vv[0] if vv else None, "vitrina_src": vv[1] if vv else None})
    return q, s, nc, v


def main():
    bad, comp, vit = bad_days(), nm_composition(), vitrina_cost()
    out, chat, store = [], [], []
    out.append("# Товар на пострадавших складах ВБ — оценка\n")
    out.append(f"Считано {dt.date.today():%d.%m.%Y}. Остаток — снимки `wb_stocks` (наши, ежедневные).\n")
    out.append("**Себест/шт — цена приёмки МойСклада, ближайшая к дате удара и не позже неё.**")
    out.append("FIFO отгрузок тут не годится: товар не продан, номер отгрузки, которой он попал")
    out.append("на склад ВБ (отмена/невыкуп), неизвестен, дата поступления на склад — тоже.")
    out.append("Карточка-набор считается по составу: цена = сумма приёмок компонентов.\n")
    totals = {}
    for status, head, note, group in (
        ("lost", "## Уничтожены полностью — утрата",
         "Товар считаем потерянным: склада нет, остаток заморожен.", TARGETS),
        ("damaged", "## Серьёзно повреждены — остаток под риском",
         "Это НЕ подтверждённая утрата: часть товара может быть цела. ВБ обещал показать "
         "пострадавший остаток отдельной колонкой «Отчёта по остаткам» (новость 16.08) — "
         "по ней и сверять.", DAMAGED)):
        out.append(head + "\n"); out.append(note + "\n")
        tq = ts = tnc = tv = 0
        for wh, label, hit, st in group:
            q, s, nc, v = wh_block(wh, label, hit, st, bad, comp, vit, out, store)
            tq += q; ts += s; tnc += nc; tv += v
            if q:
                chat.append(f"{label:<30} шт {q:>4}  приёмка {rub(s):>9} ₽   витрина {rub(v):>9} ₽"
                            + (f"  (без приёмки {nc})" if nc else ""))
        totals[status] = (tq, ts, tnc, tv)
        out.append(f"**Итого по группе:** штук {tq}, по цене приёмки **{rub(ts)} ₽**"
                   + (f", без приёмки в базе {tnc} шт." if tnc else ".")
                   + f" Для сверки, по витрине — {rub(tv)} ₽.\n")
        chat.append(f"{'  ИТОГО ' + status:<30} шт {tq:>4}  приёмка {rub(ts):>9} ₽   витрина {rub(tv):>9} ₽")
    out.append("## Оговорки\n")
    out.append("- **Цена приёмки — приближение, а не партия.** Мы не знаем, из какой поставки")
    out.append("  физически лежала каждая штука на складе ВБ, поэтому берём последнюю закупочную")
    out.append("  цену до дня удара. Для претензии это защитимая и проверяемая по документам МС цифра.")
    out.append("- **Карточка-набор считается по составу.** Половина карточек — комплекты CMYK;")
    out.append("  состав берём из фактических отгрузок МС (отправление ВБ → документ отгрузки →")
    out.append("  его позиции, самый частый состав). Набор, у которого хоть один компонент без")
    out.append("  приёмки, в сумму не включён.")
    out.append("- **Разрыв снимков 16–22.07.2026** — ВБ отключил старый эндпоинт остатков 20.07,")
    out.append("  коллектор переехал на `warehouse_remains`. По ударам 18.07 снимка на сам день нет,")
    out.append("  взят последний до удара — 15.07. По той же причине «СЦ Шушары» исчез из выгрузки")
    out.append("  после 15.07: новый эндпоинт сортировочные центры не отдаёт.")
    out.append("- **«Алексин» в наших остатках не значится.** Новость ВБ 04.08 названа «Тула», адрес")
    out.append("  инцидента в теле — Алексин. Считаем по складу ВБ «Тула».")
    out.append("- Снимок — остаток по данным ВБ на нашем аккаунте, а не акт о наличии. Претензию")
    out.append("  подавать по «Отчёту по остаткам» ВБ; наш снимок — независимая сверка.")
    out.append("- Себест — закупочная без логистики до склада и без упущенной выручки.\n")
    out.append("## Склады без нашего товара в снимках\n")
    for label, hit in ABSENT:
        out.append(f"- **{label}** (удар {dt.date.fromisoformat(hit):%d.%m.%Y}) — такого склада нет "
                   "в `wb_stocks` ни в одном снимке: нашего товара там не было.")
    lq, ls, lnc, lv = totals["lost"]; dq, ds, dnc, dv = totals["damaged"]
    out.append(f"\n## Итого\n\nУничтожено: {lq} шт на **{rub(ls)} ₽**. "
               f"Под риском на повреждённых складах: {dq} шт на **{rub(ds)} ₽**. "
               f"Вместе {lq+dq} шт на **{rub(ls+ds)} ₽** "
               f"(сверочный метод по витрине — {rub(lv+dv)} ₽).")
    path = "docs/reports/wb_lost_warehouses_2026-08-21.md"
    open(path, "w").write("\n".join(out) + "\n")
    db.execute("TRUNCATE wb_wh_loss")
    db.upsert("wb_wh_loss", store, ["warehouse", "account", "nm_id"])
    print("\n".join(chat))
    print(f"строк в витрине wb_wh_loss: {len(store)}")
    print("файл:", path)


if __name__ == "__main__":
    main()
