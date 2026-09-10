#!/usr/bin/env python3
# поток: rev
"""Стоимость движка ответов по дням и моделям — замер «до/после» перевода на Sonnet + батч.

Источник — `feedback_llm_cost_log` (day, model, calls, tokens_in, tokens_out, cost_usd): туда пишут
синхронный счётчик `feedback_today._CostTracker.persist()` и сборщик батча `llm_batch._log_cost()`.
Батчевые вызовы приходят с суффиксом « (batch)» в имени модели — так в разрезе по моделям видно,
сколько ответов собрано вполцены, а сколько ушло одиночными вызовами.

Чего здесь НЕТ: DeepSeek (веб-поиск) — у него отдельный биллинг и в этой таблице он не тарифицируется.

    ./venv/bin/python tools/rev_llm_cost.py [--days 14]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv                                                   # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
from core import db                                                              # noqa: E402
from reports import llm_pricing                                                  # noqa: E402


def main(days=14):
    rows = db.query("""SELECT day, model, calls, tokens_in, tokens_out, cost_usd
                         FROM feedback_llm_cost_log
                        WHERE day >= current_date - %s
                        ORDER BY day DESC, cost_usd DESC""", (days,))
    if not rows:
        print(f"За последние {days} дн. вызовов не записано.")
        return 0
    print(f"СТОИМОСТЬ СБОРКИ ОТВЕТОВ за {days} дн. (feedback_llm_cost_log)\n")
    print(f"{'день':<12}{'модель':<30}{'вызовов':>9}{'ток. вх':>10}{'ток. вых':>10}{'$':>9}")
    cur, sub = None, {}
    total = {"calls": 0, "cost": 0.0}

    def _flush():
        if cur and sub["calls"]:
            print(f"{'':<12}{'— всего за день':<30}{sub['calls']:>9}{'':>10}{'':>10}{sub['cost']:>9.4f}")

    for r in rows:
        if r["day"] != cur:
            _flush()
            cur, sub = r["day"], {"calls": 0, "cost": 0.0}
        print(f"{str(r['day']):<12}{r['model'][:29]:<30}{r['calls']:>9}"
              f"{r['tokens_in']:>10}{r['tokens_out']:>10}{float(r['cost_usd'] or 0):>9.4f}")
        sub["calls"] += r["calls"]
        sub["cost"] += float(r["cost_usd"] or 0)
        total["calls"] += r["calls"]
        total["cost"] += float(r["cost_usd"] or 0)
    _flush()
    print(f"\nИТОГО за период: {total['calls']} вызовов · ${total['cost']:.4f}"
          f" · ≈${total['cost'] / max(total['calls'], 1):.4f} за вызов")

    # Что было бы на прежней схеме (весь объём одиночными вызовами Opus 5) — верхняя граница
    # сравнения: токены на Opus и Sonnet отличаются незначительно, а цена — впятеро.
    op = sum(llm_pricing.cost("claude-opus-5", r["tokens_in"], r["tokens_out"]) for r in rows)
    if op:
        print(f"Тот же объём токенов на прежней схеме (Opus 5, одиночными): ${op:.4f} "
              f"— экономия ${op - total['cost']:.4f} ({100 * (1 - total['cost'] / op):.0f} %)")

    by_model = {}
    for r in rows:
        b = by_model.setdefault(llm_pricing.base_model(r["model"]), {"n": 0, "batch": 0})
        b["n"] += r["calls"]
        if llm_pricing.is_batch(r["model"]):
            b["batch"] += r["calls"]
    print("\nРаспределение вызовов по моделям:")
    for m, b in sorted(by_model.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {m:<28}{b['n']:>6} вызовов, из них батчем {b['batch']} "
              f"({100 * b['batch'] / max(b['n'], 1):.0f} %)")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(days=int(a[a.index("--days") + 1]) if "--days" in a else 14))
