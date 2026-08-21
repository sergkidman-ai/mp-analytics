#!/usr/bin/env python3
# поток: ev — оценка товара, утраченного на пострадавших складах ВБ.
# Считает остаток по снимку wb_stocks на дату удара (или ближайший доступный) и его себестоимость.
# Себест/шт — из витрины mkt_sku_economics (cogs_u + cogs_source: shipment / live / live_stale),
# она же несёт согласованную иерархию фолбэков; резерв — products.cost_seb по external_code.
# Наивный джойн external_code -> cost_seb основным источником НЕ делаем (CLAUDE.md п.5).
import sys, datetime as dt
from collections import Counter
from core import db

# склад в wb_stocks -> (имя из новостей, дата удара, статус)
TARGETS = [
    ("Электросталь",           "Электросталь",         "2026-07-18", "lost"),
    ("Котовск",                "Котовск",              "2026-07-18", "lost"),
    ("Владимир",               "Владимир: Воршинское", "2026-07-25", "lost"),
    ("Самара (Новосемейкино)", "Новосемейкино",        "2026-08-02", "lost"),
    ("Тула",                   "Алексин (склад ВБ «Тула»)", "2026-08-04", "lost"),
]
# уничтожены, но нашего товара там нет ни в одном снимке
ABSENT = [("Чехов / Новосёлки", "2026-08-16"), ("Северное Домодедово", "2026-08-16")]


def cost_map():
    """nm_id -> (себест/шт, источник)."""
    m = {}
    for r in db.query("""SELECT DISTINCT ON (nm_id) nm_id, cogs_u, cogs_source
                         FROM mkt_sku_economics WHERE cogs_u > 0 ORDER BY nm_id, built_at DESC"""):
        m[str(r["nm_id"])] = (float(r["cogs_u"]), r["cogs_source"])
    for r in db.query("""SELECT DISTINCT ON (c.nm_id) c.nm_id, p.cost_seb, p.buy_price
                         FROM wb_cards c JOIN products p ON p.external_code = c.vendor_code
                         WHERE COALESCE(p.cost_seb, p.buy_price) > 0"""):
        m.setdefault(str(r["nm_id"]), (float(r["cost_seb"] or r["buy_price"]), "ms_card"))
    return m


def bad_days():
    """Дни с обрезанным снимком: строк заметно меньше, чем у соседних дней (окно +-3).
    Объём выгрузки ВБ менялся скачками (смена эндпоинта 20.07), поэтому сравнение только локальное."""
    rows = db.query("SELECT captured_at::date d, count(*) n FROM wb_stocks GROUP BY 1 ORDER BY 1")
    bad = set()
    for i, r in enumerate(rows):
        near = sorted(x["n"] for x in rows[max(0, i - 3):i + 4] if x is not r)
        if near and r["n"] < 0.7 * near[len(near) // 2]:
            bad.add(r["d"])
    return bad


def snapshot_date(wh, hit):
    """Дата снимка: сам день удара, иначе ближайший ДО него, иначе ближайший после."""
    for order, sign in (("DESC", "<="), ("ASC", ">")):
        r = db.query(f"""SELECT captured_at::date d FROM wb_stocks
                         WHERE warehouse=%s AND captured_at::date {sign} %s::date
                         ORDER BY captured_at {order} LIMIT 1""", (wh, hit))
        if r:
            return r[0]["d"], ("день удара" if str(r[0]["d"]) == hit else
                               ("ближайший до" if sign == "<=" else "ближайший после"))
    return None, None


def main():
    global BAD
    BAD = bad_days()
    costs = cost_map()
    out, chat = [], []
    out.append("# Товар на уничтоженных складах ВБ — оценка утраты\n")
    out.append(f"Считано {dt.date.today():%d.%m.%Y}. Источник остатка — снимки `wb_stocks` (наши, ежедневные).")
    out.append("Себест/шт — `mkt_sku_economics.cogs_u` (источник в колонке: shipment — по документу отгрузки, live — живая закупка, live_stale — устаревшая живая, ms_card — карточка МС).\n")
    total_q = total_s = total_nc = 0
    for wh, label, hit, st in TARGETS:
        d, how = snapshot_date(wh, hit)
        while d in BAD:
            d, how = snapshot_date(wh, str(d - dt.timedelta(days=1)))
        if not d:
            out.append(f"## {label}\nСнимков нет.\n"); continue
        rows = db.query("""SELECT account, nm_id, vendor_code, sum(quantity) q
                           FROM wb_stocks WHERE warehouse=%s AND captured_at::date=%s
                           GROUP BY 1,2,3 HAVING sum(quantity)>0 ORDER BY 4 DESC""", (wh, d))
        q = sum(int(r["q"]) for r in rows)
        priced = [(r, costs[str(r["nm_id"])][0]) for r in rows if str(r["nm_id"]) in costs]
        s = sum(int(r["q"]) * c for r, c in priced)
        nc = q - sum(int(r["q"]) for r, _ in priced)
        total_q += q; total_s += s; total_nc += nc
        lag = (dt.date.fromisoformat(hit) - d).days
        out.append(f"## {label} — удар {dt.date.fromisoformat(hit):%d.%m.%Y}")
        out.append(f"Снимок {d:%d.%m.%Y} ({how}" + (f", разрыв {abs(lag)} дн." if lag else "") + ")")
        out.append(f"Штук {q}, SKU {len(rows)}, себест **{s:,.0f} ₽**".replace(",", " ") +
                   (f", без себеста {nc} шт" if nc else ""))
        src = Counter(costs[str(r["nm_id"])][1] for r, _ in priced)
        out.append("Источник себеста: " + ", ".join(f"{k} {v}" for k, v in src.most_common()))
        out.append("\n| Аккаунт | nmID | Артикул | Шт | Себест/шт | Сумма | Источник |\n|---|---|---|---|---|---|---|")
        for r, c in sorted(priced, key=lambda x: -int(x[0]["q"]) * x[1]):
            out.append(f"| {r['account']} | {r['nm_id']} | {r['vendor_code']} | {int(r['q'])} | {c:,.0f} | {int(r['q'])*c:,.0f} | {costs[str(r['nm_id'])][1]} |".replace(",", " "))
        for r in rows:
            if str(r["nm_id"]) not in costs:
                out.append(f"| {r['account']} | {r['nm_id']} | {r['vendor_code']} | {int(r['q'])} | — | — | нет |")
        out.append("")
        # контроль: снимок после удара и «замер ли» остаток (склад не отгружает — косвенный признак утраты)
        after = db.query("""SELECT captured_at::date d, sum(quantity) q FROM wb_stocks
                            WHERE warehouse=%s AND captured_at::date > %s::date
                            GROUP BY 1 ORDER BY 1""", (wh, hit))
        after = [a for a in after if a["d"] not in BAD]
        if after:
            vals = [int(a["q"]) for a in after]
            move = sum(abs(vals[i] - vals[i-1]) for i in range(1, len(vals)))
            out.append(f"\nКонтроль по снимкам после удара: {after[0]['d']:%d.%m} — {vals[0]} шт, "
                       f"последний снимок {after[-1]['d']:%d.%m} — {vals[-1]} шт; "
                       f"суммарное движение за {len(after)} дн. {move} шт "
                       + ("(остаток фактически заморожен — склад не отгружает)." if move <= 2 else "(остаток шевелится)."))
            gone = db.query("SELECT max(captured_at)::date d FROM wb_stocks WHERE warehouse=%s", (wh,))[0]["d"]
            last = db.query("SELECT max(captured_at)::date d FROM wb_stocks")[0]["d"]
            if gone < last:
                out.append(f"С {gone:%d.%m.%Y} склад из отчёта ВБ пропал — в свежих снимках его нет (последний снимок базы {last:%d.%m.%Y}).")
        chat.append(f"{label:<28} {d:%d.%m}  шт {q:>4}  себест {s:>9,.0f} ₽".replace(",", " ") + (f"  (без цены {nc})" if nc else ""))
    out.append("## Оговорки\n")
    out.append("- **Разрыв снимков 16–22.07.2026** — ВБ отключил старый эндпоинт остатков 20.07, коллектор переехал")
    out.append("  на `warehouse_remains`. По ударам 18.07 (Электросталь, Котовск) снимка на сам день нет,")
    out.append("  взят последний до удара — 15.07.")
    out.append("- **«Алексин» в наших остатках не значится.** Новость ВБ 04.08 названа «Тула», адрес инцидента в теле —")
    out.append("  Алексин. Считаем по складу ВБ «Тула»; если это разные объекты, строку надо снять.")
    out.append("- Снимок — остаток на нашем аккаунте по данным ВБ, а не акт о наличии. Заявляя претензию, опираться")
    out.append("  на «Отчёт по остаткам» ВБ на дату удара; наш снимок — независимая сверка.")
    out.append("- Себест — закупочная, без логистики до склада и без упущенной выручки.\n")
    out.append("## Склады без нашего товара в снимках\n")
    for label, hit in ABSENT:
        out.append(f"- **{label}** (удар {dt.date.fromisoformat(hit):%d.%m.%Y}) — в `wb_stocks` такого склада нет ни в одном снимке: нашего товара там не было.")
    out.append(f"\n## Итого\n\nШтук {total_q}, себест **{total_s:,.0f} ₽**".replace(",", " ") +
               (f", без себеста {total_nc} шт." if total_nc else "."))
    path = "docs/reports/wb_lost_warehouses_2026-08-21.md"
    open(path, "w").write("\n".join(out) + "\n")
    print("\n".join(chat))
    print(f"ИТОГО                        шт {total_q:>4}  себест {total_s:>9,.0f} ₽".replace(",", " "), f"| без цены {total_nc} шт")
    print("файл:", path)

if __name__ == "__main__":
    main()
