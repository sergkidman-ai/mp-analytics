# поток: ev
"""ya_removal_candidates.py — кандидаты на вывоз со склада Яндекс.Маркета (FBY).

Повод (новость Маркета, действует с 01.09.2026): у каждого товара фиксированный срок льготного
хранения от даты поступления поставки на склад, оборачиваемость на стоимость больше не влияет.
Бесплатно: КГТ 30 дней, одежда/обувь 365, ВСЁ ОСТАЛЬНОЕ (наши картриджи) — 120 дней.
Дальше платно: КГТ 0,25 ₽ за литр в день, остальное 2,5 ₽ за литр в день; предварительный
расчёт Маркета учитывает скидку 90 %.

Логика та же, что у Ozon FBO (reports/ozon_removal_candidates.py): предлагаем вывезти то, что
не продаётся и вот-вот начнёт стоить денег. Отличие в гейте: у Маркета порог задаёт не наш
норматив застоя, а его же льготный срок — предупреждаем за LEAD_DAYS до конца льготы.

Источник данных — снимок ya_fby_stock (collectors/yandex_stocks.py). Пока в ЛК Маркета выключен
доступ к API FBY-магазина, снимков нет и отчёт честно говорит об этом, а не молчит.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

FREE_DAYS = 120         # льготное хранение для наших категорий, дней (с 01.09.2026)
LEAD_DAYS = 30          # за сколько дней до конца льготы поднимать позицию
STALE_DAYS = 60         # застой без продаж, при котором вывозим независимо от льготы
RATE_RUB_L_DAY = 2.5    # ₽ за литр в день после льготы (до скидки 90 %)
DISCOUNT = 0.9          # анонсированная скидка на хранение
TARIFF_FROM = "2026-09-01"


def _volume_l(offer_id):
    """Объём короба в литрах из наших габаритов; None, если размера нет."""
    r = db.query("""SELECT length_mm, width_mm, height_mm FROM ms_product
                    WHERE external_code=%s AND length_mm IS NOT NULL LIMIT 1""", (offer_id,))
    if not r or not all((r[0]["length_mm"], r[0]["width_mm"], r[0]["height_mm"])):
        return None
    return round(r[0]["length_mm"] * r[0]["width_mm"] * r[0]["height_mm"] / 1_000_000, 2)


def build():
    """Кандидаты по последнему снимку FBY: [{offer_id, qty, days, reason, storage_rub_month}]."""
    day = db.query("SELECT max(captured_at) d FROM ya_fby_stock")
    day = day[0]["d"] if day else None
    if not day:
        return None, []
    rows = db.query("""SELECT offer_id, warehouse, warehouse_id, available, frozen,
                              turnover_days, turnover, updated_at
                       FROM ya_fby_stock WHERE captured_at=%s AND available+frozen > 0
                       ORDER BY offer_id""", (day,))
    out = []
    for r in rows:
        qty = (r["available"] or 0) + (r["frozen"] or 0)
        days = r["turnover_days"]
        reasons = []
        if days is not None and days >= FREE_DAYS - LEAD_DAYS:
            reasons.append(f"льгота кончается: лежит {days}д из {FREE_DAYS}")
        if days is not None and days >= STALE_DAYS and not reasons:
            reasons.append(f"застой {days}д")
        if not reasons:
            continue
        vol = _volume_l(r["offer_id"])
        cost = None
        if vol:
            cost = round(vol * qty * RATE_RUB_L_DAY * (1 - DISCOUNT) * 30, 2)
        out.append({"offer_id": r["offer_id"], "warehouse": r["warehouse"] or r["warehouse_id"],
                    "qty": qty, "days": days, "reason": "; ".join(reasons),
                    "volume_l": vol, "storage_rub_month": cost})
    return day, out


def format_report():
    """Текстовый блок для еженедельной рассылки (вторник)."""
    day, rows = build()
    head = "📦 Вывоз со склада Яндекс.Маркета (FBY)"
    if day is None:
        return (f"{head}\n"
                f"⚠️ Данных нет: в ЛК Маркета выключен доступ к API магазина «Цифровой квадрат» (FBY).\n"
                f"Включить: Настройки → Доступ к API. После этого остатки поедут сами.\n"
                f"Зачем срочно: с {TARIFF_FROM} хранение платное после {FREE_DAYS} дней "
                f"({RATE_RUB_L_DAY} ₽/л/день до скидки {int(DISCOUNT*100)} %).")
    if not rows:
        return f"🟢 {head} — на {day} кандидатов нет."
    total = sum(r["qty"] for r in rows)
    known = [r["storage_rub_month"] for r in rows if r["storage_rub_month"] is not None]
    out = [f"{head} — на {day}",
           f"К вывозу {len(rows)} поз., {total} шт."
           + (f" Хранение ≈ {round(sum(known))} ₽/мес после льготы." if known else ""),
           "Оформить: ЛК Маркета → Товары → Остатки → «Вывезти со склада».", ""]
    for r in rows:
        cost = f" · ≈{r['storage_rub_month']} ₽/мес" if r["storage_rub_month"] is not None else ""
        out.append(f"  • {r['offer_id']} ×{r['qty']} — {r['reason']}{cost}")
    out.append("")
    out.append(f"Правила: льгота {FREE_DAYS}д с поставки (предупреждаем за {LEAD_DAYS}д) · "
               f"застой ≥{STALE_DAYS}д · тариф с {TARIFF_FROM}.")
    return "\n".join(out)


if __name__ == "__main__":
    print(format_report())
