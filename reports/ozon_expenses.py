"""reports/ozon_expenses.py — витрина расходов Ozon из raw_ozon_transaction.

Читает сырьё (payload JSONB), раскладывает каждую операцию по статьям через
collectors.ozon.categorize_operation (без двойного счёта, Σкатегорий == amount).

Три разреза:
  1) Помесячно за период (по умолчанию полгода) — все статьи расходов.
  2) Июнь по НЕДЕЛЯМ (Пн–Вс, как отчёт WB).
  3) FBO vs FBS по posting.delivery_schema — Ozon видит схему, а в МС FBO-продаж НЕТ
     (товар уходит со склада Ozon, отгрузки в МС не создаётся — как WB-FBO).

Запуск:  ./venv/bin/python reports/ozon_expenses.py [oz_acc1] [2026-01-01] [2026-06-30]
"""
import sys
import pathlib
import datetime
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import categorize_operation, CATEGORIES  # noqa: E402

# что показываем как расходные статьи (revenue выносим отдельной строкой сверху)
COST_CATS = [c for c in CATEGORIES if c != "revenue"]
RU = {"revenue": "Выручка", "commission": "Комиссия", "advertising": "Реклама/продвиж.",
      "logistics": "Логистика", "returns": "Возвраты", "penalties": "Штрафы",
      "acquiring": "Эквайринг", "storage": "Склад/обработка", "subscription": "Подписка",
      "partners": "Партнёрские", "points": "Баллы/Звёздные", "compensation": "Компенсации",
      "fbo": "FBO склад", "other": "Прочее"}


def _fmt(v):
    return f"{v:>14,.0f}".replace(",", " ")


def _rows(account, date_from, date_to):
    """Список payload-словарей операций за период (по operation_date)."""
    return [r["payload"] for r in db.query(
        """SELECT payload FROM raw_ozon_transaction
           WHERE account=%s AND (payload->>'operation_date')::date BETWEEN %s AND %s""",
        (account, date_from, date_to))]


def _schema(op):
    s = ((op.get("posting") or {}).get("delivery_schema") or "").strip()
    return s.upper() if s else "—"


def _sum(ops):
    """Σ по категориям для набора операций."""
    tot = {c: 0.0 for c in CATEGORIES}
    for op in ops:
        for c, v in categorize_operation(op).items():
            tot[c] += v
    return tot


def _print_breakdown(title, buckets, order):
    """buckets: {col_key: {cat: sum}}; order — порядок колонок."""
    print(f"\n=== {title} ===")
    head = "Статья".ljust(18) + "".join(k.rjust(15) for k in order) + "ИТОГО".rjust(15)
    print(head)
    print("-" * len(head))
    totals = {k: buckets[k] for k in order}
    grand = {c: sum(totals[k][c] for k in order) for c in CATEGORIES}
    for cat in ["revenue"] + COST_CATS:
        if abs(grand[cat]) < 1:
            continue
        line = RU[cat].ljust(18)
        for k in order:
            line += _fmt(totals[k][cat]).rjust(15)
        line += _fmt(grand[cat]).rjust(15)
        print(line)
    # к перечислению = Σ всех категорий (== Σ amount)
    print("-" * len(head))
    netline = "К перечислению".ljust(18)
    for k in order:
        netline += _fmt(sum(totals[k].values())).rjust(15)
    netline += _fmt(sum(grand.values())).rjust(15)
    print(netline)


def monthly(account, date_from, date_to):
    """Помесячная разбивка за период."""
    start = datetime.date.fromisoformat(date_from)
    end = datetime.date.fromisoformat(date_to)
    months, cur = [], start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        m_from = max(cur, start)
        m_to = min(nxt - datetime.timedelta(days=1), end)
        months.append((cur.strftime("%Y-%m"), m_from.isoformat(), m_to.isoformat()))
        cur = nxt
    buckets, order = {}, []
    for label, mf, mt in months:
        buckets[label] = _sum(_rows(account, mf, mt))
        order.append(label)
    _print_breakdown(f"РАСХОДЫ Ozon {account} ПО МЕСЯЦАМ ({date_from}..{date_to})", buckets, order)


def weekly_june(account, year=2026):
    """Июнь по неделям Пн–Вс (как отчёт WB)."""
    by_week = defaultdict(list)
    for op in _rows(account, f"{year}-06-01", f"{year}-06-30"):
        d = datetime.date.fromisoformat((op.get("operation_date") or "")[:10])
        wk_start = d - datetime.timedelta(days=d.weekday())  # понедельник
        by_week[wk_start].append(op)
    buckets, order = {}, []
    for wk in sorted(by_week):
        wk_end = wk + datetime.timedelta(days=6)
        label = f"{wk.strftime('%d.%m')}-{wk_end.strftime('%d.%m')}"
        buckets[label] = _sum(by_week[wk])
        order.append(label)
    _print_breakdown(f"РАСХОДЫ Ozon {account} ИЮНЬ {year} ПО НЕДЕЛЯМ (Пн–Вс)", buckets, order)


def fbo_vs_fbs(account, date_from, date_to):
    """Разрез FBO/FBS по delivery_schema — то, чего НЕ видно в МС."""
    by_schema = defaultdict(list)
    for op in _rows(account, date_from, date_to):
        by_schema[_schema(op)].append(op)
    order = [s for s in ("FBO", "FBS", "RFBS", "CROSSBORDER", "—") if s in by_schema]
    order += [s for s in by_schema if s not in order]
    buckets = {s: _sum(by_schema[s]) for s in order}
    _print_breakdown(f"Ozon {account} FBO vs FBS ({date_from}..{date_to}) — в МС FBO НЕ виден",
                     buckets, order)
    # доля FBO в выручке
    rev = {s: buckets[s]["revenue"] for s in order}
    total_rev = sum(rev.values()) or 1
    print("\n  доля выручки по схеме:")
    for s in order:
        if abs(rev[s]) >= 1:
            print(f"    {s:<12} {_fmt(rev[s])}  ({rev[s]/total_rev*100:5.1f}%)")


def main(account="oz_acc1", date_from="2026-01-01", date_to="2026-06-30"):
    monthly(account, date_from, date_to)
    weekly_june(account)
    fbo_vs_fbs(account, date_from, date_to)


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "oz_acc1",
         a[2] if len(a) > 2 else "2026-01-01",
         a[3] if len(a) > 3 else "2026-06-30")
