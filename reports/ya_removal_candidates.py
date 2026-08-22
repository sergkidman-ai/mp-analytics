# поток: ev
"""ya_removal_candidates.py — блок Яндекс.Маркета в еженедельном списке «что забрать со складов МП».

Переписан 22.08.2026 после проверки фактов (правка Натальи):

* На Маркете НАШ товар на складе Маркета не лежит: FBY-магазин пустой (и API у него выключен),
  а по семи FBS-магазинам склад наш собственный — вывозить оттуда нечего.
* У Маркета оседает только то, что вернулось: невыкупы и отмены. Их Маркет почти всегда сразу
  везёт нам на ПВЗ — за всю историю услуга «Хранение невыкупов и возвратов» стоила 630 ₽
  (15 ₽ за штуку, в августе 2026 — ноль).
* Значит предмет еженедельного напоминания по Маркету — не «вывоз со склада», а «забрать
  возврат с ПВЗ, пока не просрочен» плюс сторож на случай, если хранение вдруг начнёт капать.

Возвраты собирает поток `ret` (returns_bot → mp_returns); отсюда читаем их только на чтение.
Остатки складов — ya_mp_stock (collectors/yandex_stocks.py): если на FBY когда-нибудь появится
товар, он попадёт в блок вывоза по правилам льготного хранения Маркета.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

FREE_DAYS = 120         # льгота хранения FBY для наших категорий (тариф Маркета с 01.09.2026)
LEAD_DAYS = 30          # за сколько дней до конца льготы поднимать позицию
RATE_RUB_L_DAY = 2.5    # ₽ за литр в день после льготы (до скидки 90 %)
TARIFF_FROM = "2026-09-01"
STORAGE_SERVICES = ("Хранение невыкупов и возвратов", "storage_of_returns")


def pickup_pending():
    """Возвраты Маркета, которые физически ждут нас (не закрыты)."""
    return db.query("""
        SELECT return_id, campaign, status_name, stage, pvz_name, pvz_address,
               created_at::date AS created, deadline_at::date AS deadline,
               (CURRENT_DATE - created_at::date) AS age
        FROM mp_returns
        WHERE platform='yandex' AND stage NOT IN ('closed')
          AND created_at >= CURRENT_DATE - INTERVAL '120 days'
        ORDER BY deadline_at NULLS LAST, created_at""")


def storage_charges(months=3):
    """Плата за хранение невыкупов и возвратов по месяцам — сторож «начало капать»."""
    return db.query("""
        SELECT ym, count(*) AS n, round(sum(cost)::numeric, 2) AS rub
        FROM raw_yandex_services WHERE service = ANY(%s)
        GROUP BY ym ORDER BY ym DESC LIMIT %s""", (list(STORAGE_SERVICES), months))


def fby_stuck():
    """Товар на складе Маркета (FBY), у которого кончается льготное хранение."""
    day = db.query("SELECT max(captured_at) d FROM ya_mp_stock WHERE placement='FBY'")
    day = day[0]["d"] if day else None
    if not day:
        return None, []
    rows = db.query("""SELECT offer_id, warehouse, available+frozen AS qty, turnover_days
                       FROM ya_mp_stock
                       WHERE captured_at=%s AND placement='FBY' AND available+frozen > 0
                         AND turnover_days >= %s
                       ORDER BY turnover_days DESC""", (day, FREE_DAYS - LEAD_DAYS))
    return day, rows


def format_report():
    """Текстовый блок для еженедельной рассылки (вторник)."""
    out = ["📦 Яндекс.Маркет — что у него лежит нашего"]

    pend = pickup_pending()
    ready = [r for r in pend if r["stage"] == "pickup"]
    stuck = [r for r in pend if r["stage"] == "attention"]
    transit = [r for r in pend if r["stage"] not in ("pickup", "attention")]
    if ready:
        out.append(f"🚚 Забрать с ПВЗ: {len(ready)} шт.")
        for r in ready:
            dl = f", до {r['deadline']}" if r["deadline"] else ""
            out.append(f"  • возврат {r['return_id']} · {r['campaign']} · {r['status_name']}"
                       f" · {r['pvz_name'] or 'ПВЗ не указан'}{dl}")
    if stuck:
        out.append(f"⚠️ Разобраться: {len(stuck)} шт.")
        for r in stuck:
            out.append(f"  • возврат {r['return_id']} · {r['campaign']} · {r['status_name']}"
                       f" · с {r['created']}")
    if transit:
        out.append(f"⏳ В пути к нам: {len(transit)} шт. (забирать пока нечего)")
    if not pend:
        out.append("🟢 Возвратов на руках у Маркета нет.")

    stor = storage_charges()
    if stor and stor[0]["rub"]:
        out.append(f"💸 Хранение невыкупов — последнее начисление {stor[0]['ym']}: "
                   f"{stor[0]['rub']} ₽ ({stor[0]['n']} шт.). Обычно ноль; растёт — значит "
                   f"возвраты залипают у Маркета.")

    day, fby = fby_stuck()
    if fby:
        out.append(f"🏬 Склад Маркета (FBY), снимок {day} — льгота {FREE_DAYS} дн. кончается:")
        for r in fby:
            out.append(f"  • {r['offer_id']} ×{r['qty']} — лежит {r['turnover_days']} дн. "
                       f"({r['warehouse']})")
        out.append(f"  Оформить: ЛК Маркета → Товары → Остатки → «Вывезти со склада». "
                   f"С {TARIFF_FROM} после льготы {RATE_RUB_L_DAY} ₽/л/день (скидка 90 %).")
    elif day is None:
        out.append("🏬 Склад Маркета (FBY): нашего товара там нет — вывозить нечего.")

    return "\n".join(out)


if __name__ == "__main__":
    print(format_report())
