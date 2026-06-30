"""analyze_jam.py — разбор данных WB Джем (позиции + поисковые запросы).

Сводит wb_search_summary / wb_search_report / wb_search_text и наш список выпавших SKU
(docs/откат_цен_*.txt) в один отчёт docs/jam_positions.txt для просмотра через less.

Семантика: позиция — чем МЕНЬШЕ, тем выше в выдаче. Для позиций dynamics — изменение пунктов
(минус = поднялись, плюс = упали ниже). Для воронки (orders/openCard/...) dynamics — % к прошлой неделе.

Запуск:  ./venv/bin/python analyze_jam.py
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "jam_positions.txt"
ACCS = {"wb_acc1": "ЦИФРОВОЙ КВАДРАТ", "wb_acc2": "ДИСКВЭР"}


def fallout_nmids():
    """nmID из откат-файлов → имя нашей цели по аккаунтам."""
    res = {"wb_acc1": [], "wb_acc2": []}
    for acc, fname in (("wb_acc1", "rollback_digital.txt"), ("wb_acc2", "rollback_diskver.txt")):
        p = BASE_DIR / "docs" / fname
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(\d{6,})\s", line)
            if m:
                res[acc].append(int(m.group(1)))
    return res


def fmt_dyn(v, pct=False, invert=False):
    if v is None:
        return "  —"
    arrow = ""
    sv = v
    if invert:   # для позиций: рост числа = хуже
        good = v < 0
    else:
        good = v > 0
    if v > 0:
        arrow = "▲"
    elif v < 0:
        arrow = "▼"
    tag = "" if v == 0 else ("+" if v > 0 else "")
    suf = "%" if pct else ""
    mark = "" if v == 0 else ("  ок" if good else "  ⚠")
    return f"{arrow}{tag}{v}{suf}{mark}"


def w(f, s=""):
    f.write(s + "\n")


def section_summary(f):
    rows = db.query("SELECT * FROM wb_search_summary ORDER BY account")
    w(f, "=" * 96)
    w(f, "1. СВОДКА ПО АККАУНТАМ (позиция в выдаче — чем меньше, тем выше; динамика к прошлой неделе)")
    w(f, "=" * 96)
    for r in rows:
        w(f, f"\n● {ACCS.get(r['account'], r['account'])}  ({r['period_start']} … {r['period_end']})")
        w(f, f"    Рейтинг продавца:   {r['supplier_rating']}")
        w(f, f"    Товаров всего:      {r['total_products']}   рекламируется: {r['advertised']}")
        w(f, f"    Средняя позиция:    {r['avg_position']}   ({fmt_dyn(r['avg_position_dyn'], invert=True)} пунктов)")
        w(f, f"    Медианная позиция:  {r['median_position']}   ({fmt_dyn(r['median_position_dyn'], invert=True)} пунктов)")
        w(f, f"    В топ-100:          {r['first_hundred']} товаров ({fmt_dyn(r['first_hundred_dyn'])})")
        w(f, f"    Видимость:          {r['visibility']}   ({fmt_dyn(r['visibility_dyn'])})")
        w(f, f"    Показы карточек:    {r['open_card']}   ({fmt_dyn(r['open_card_dyn'])} к прошл.нед)")


def section_fallout(f):
    fo = fallout_nmids()
    w(f, "\n" + "=" * 96)
    w(f, "2. НАШИ ВЫПАВШИЕ SKU — подтверждает ли Джем падение позиций/заказов после роста цены")
    w(f, "=" * 96)
    have = {r["account"] for r in db.query("SELECT DISTINCT account FROM wb_search_report")}
    for acc, nmids in fo.items():
        if not nmids:
            continue
        if acc not in have:
            w(f, f"\n● {ACCS.get(acc, acc)} — Джем НЕ подключён на этом аккаунте (API 403). "
                 f"Подписка только на Цифровом. Позиции по {len(nmids)} SKU отката недоступны.")
            continue
        w(f, f"\n● {ACCS.get(acc, acc)} — {len(nmids)} SKU из списка отката:")
        w(f, f"  {'nmID':>11} {'поз':>4} {'Δпоз':>7} {'заказы':>7} {'Δзак%':>7} {'видим':>6} {'показы':>7}  товар")
        w(f, "  " + "-" * 92)
        rows = db.query("""
            SELECT nm_id, name, avg_position, avg_position_dyn, orders, orders_dyn,
                   visibility, open_card
            FROM wb_search_report WHERE account=%s AND nm_id = ANY(%s)
            ORDER BY open_card DESC NULLS LAST
        """, (acc, nmids))
        seen = {r["nm_id"] for r in rows}
        for r in rows:
            nm = (r["name"] or "")[:34]
            w(f, f"  {r['nm_id']:>11} {str(r['avg_position'] or '—'):>4} "
                 f"{fmt_dyn(r['avg_position_dyn'], invert=True):>7} {str(r['orders'] or 0):>7} "
                 f"{fmt_dyn(r['orders_dyn'], pct=True):>7} {str(r['visibility'] or '—'):>6} "
                 f"{str(r['open_card'] or 0):>7}  {nm}")
        missing = [n for n in nmids if n not in seen]
        if missing:
            w(f, f"  · нет в отчёте Джема (низкий трафик/не в выдаче): {len(missing)} шт — {missing[:12]}")


def section_worst_positions(f):
    w(f, "\n" + "=" * 96)
    w(f, "3. ХУДШИЕ ПРОСАДКИ ПОЗИЦИЙ (весь аккаунт): трафик есть, позиция упала и/или заказы просели")
    w(f, "=" * 96)
    for acc in ACCS:
        rows = db.query("""
            SELECT nm_id, name, avg_position, avg_position_dyn, orders, orders_dyn, open_card, min_price
            FROM wb_search_report
            WHERE account=%s AND open_card >= 10
              AND (avg_position_dyn > 0 OR orders_dyn < 0)
            ORDER BY (COALESCE(avg_position_dyn,0) - COALESCE(orders_dyn,0)) DESC
            LIMIT 25
        """, (acc,))
        if not rows:
            continue
        w(f, f"\n● {ACCS.get(acc, acc)}:")
        w(f, f"  {'nmID':>11} {'поз':>4} {'Δпоз':>7} {'Δзак%':>7} {'показы':>7} {'цена':>7}  товар")
        w(f, "  " + "-" * 92)
        for r in rows:
            w(f, f"  {r['nm_id']:>11} {str(r['avg_position'] or '—'):>4} "
                 f"{fmt_dyn(r['avg_position_dyn'], invert=True):>7} {fmt_dyn(r['orders_dyn'], pct=True):>7} "
                 f"{str(r['open_card'] or 0):>7} {str(r['min_price'] or '—'):>7}  {(r['name'] or '')[:34]}")


def section_quick_wins(f):
    w(f, "\n" + "=" * 96)
    w(f, "4. БЫСТРЫЕ ПОБЕДЫ ПО ЗАПРОСАМ: мы на стр.1 (поз 4–15), частый запрос, хорошая конверсия →")
    w(f, "   малый импульс (ставка/цена) = прорыв в топ-3")
    w(f, "=" * 96)
    for acc in ACCS:
        rows = db.query("""
            SELECT t.text, t.nm_id, r.name, t.avg_position, t.avg_position_dyn,
                   t.week_frequency, t.open_card, t.cart_to_order, t.orders
            FROM wb_search_text t LEFT JOIN wb_search_report r
              ON r.account=t.account AND r.period_start=t.period_start AND r.nm_id=t.nm_id
            WHERE t.account=%s AND t.avg_position BETWEEN 4 AND 15
              AND t.week_frequency >= 20
            ORDER BY t.week_frequency DESC LIMIT 30
        """, (acc,))
        if not rows:
            continue
        w(f, f"\n● {ACCS.get(acc, acc)}:")
        w(f, f"  {'поз':>4} {'Δпоз':>7} {'частота/нед':>11} {'конв%':>6}  запрос  →  товар")
        w(f, "  " + "-" * 92)
        for r in rows:
            w(f, f"  {str(r['avg_position']):>4} {fmt_dyn(r['avg_position_dyn'], invert=True):>7} "
                 f"{str(r['week_frequency'] or 0):>11} {str(r['cart_to_order'] or 0):>6}  "
                 f"{(r['text'] or '')[:40]}  →  {(r['name'] or '')[:28]}")


def section_lost_queries(f):
    w(f, "\n" + "=" * 96)
    w(f, "5. ЗАПРОСЫ, ГДЕ НАС ВЫТЕСНИЛИ СИЛЬНЕЕ ВСЕГО (конкуренты): позиция по запросу упала, частота есть")
    w(f, "=" * 96)
    for acc in ACCS:
        rows = db.query("""
            SELECT t.text, t.nm_id, r.name, t.avg_position, t.avg_position_dyn, t.week_frequency, t.orders_dyn
            FROM wb_search_text t LEFT JOIN wb_search_report r
              ON r.account=t.account AND r.period_start=t.period_start AND r.nm_id=t.nm_id
            WHERE t.account=%s AND t.avg_position_dyn > 0 AND t.week_frequency >= 15
            ORDER BY t.avg_position_dyn DESC LIMIT 25
        """, (acc,))
        if not rows:
            continue
        w(f, f"\n● {ACCS.get(acc, acc)}:")
        w(f, f"  {'поз':>4} {'Δпоз':>7} {'частота/нед':>11}  запрос  →  товар")
        w(f, "  " + "-" * 92)
        for r in rows:
            w(f, f"  {str(r['avg_position']):>4} {fmt_dyn(r['avg_position_dyn'], invert=True):>7} "
                 f"{str(r['week_frequency'] or 0):>11}  {(r['text'] or '')[:40]}  →  {(r['name'] or '')[:28]}")


def main():
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "РАЗБОР WB ДЖЕМ — позиции в выдаче и поисковые запросы")
        w(f, "Чем меньше позиция — тем выше товар. Δпоз: ▼ = поднялись (хорошо), ▲ = упали ниже (плохо).")
        w(f, "Δзак% — изменение заказов к прошлой неделе.")
        section_summary(f)
        section_fallout(f)
        section_worst_positions(f)
        section_quick_wins(f)
        section_lost_queries(f)
        w(f, "\n— конец —")
    print(f"Готово: {OUT}")


if __name__ == "__main__":
    main()
