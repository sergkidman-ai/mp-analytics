"""Сводка по собранным поисковым запросам Ozon: покрытие, объём, топ фраз.

Запуск: ./venv/bin/python tools/ozon_search_summary.py [топ_N]
"""
import sys

sys.path.insert(0, "/opt/mp-analytics")
from core import db  # noqa: E402

TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def main():
    print("=" * 78)
    print("ПРОГОНЫ ПО НЕДЕЛЯМ")
    print("=" * 78)
    rows = db.query("""
        SELECT account, period_start, period_end, skus_total, skus_with_data,
               skus_detailed, queries_rows, api_calls, tail_dropped
        FROM ozon_search_run ORDER BY account, period_start
    """)
    print(f"{'аккаунт':<9}{'неделя':<24}{'SKU всего':>10}{'с данными':>11}"
          f"{'детализ.':>10}{'строк фраз':>12}{'вызовов':>9}{'потеряно':>10}")
    for r in rows:
        per = f"{r['period_start']}..{r['period_end']}"
        print(f"{r['account']:<9}{per:<24}{r['skus_total']:>10}{r['skus_with_data']:>11}"
              f"{r['skus_detailed']:>10}{r['queries_rows']:>12}{r['api_calls']:>9}"
              f"{r['tail_dropped']:>10}")

    print()
    print("=" * 78)
    print("ПОКРЫТИЕ")
    print("=" * 78)
    for r in db.query("""
        SELECT account,
               count(DISTINCT sku)   AS sku_с_фразами,
               count(DISTINCT query) AS уник_фраз,
               count(*)              AS строк,
               min(period_start)     AS с,
               max(period_end)       AS по,
               sum(unique_view_users) AS показов,
               sum(order_count)      AS заказов
        FROM ozon_search_query GROUP BY account ORDER BY account
    """):
        print(f"{r['account']}: SKU с фразами {r['sku_с_фразами']}, "
              f"уникальных фраз {r['уник_фраз']}, строк {r['строк']}, "
              f"период {r['с']}..{r['по']}, показов {r['показов']}, "
              f"заказов {r['заказов']}")
    for r in db.query("""
        SELECT account, count(DISTINCT sku) AS всего_sku,
               count(DISTINCT sku) FILTER (WHERE unique_search_users > 0) AS с_трафиком
        FROM ozon_search_product GROUP BY account ORDER BY account
    """):
        print(f"{r['account']}: SKU в сводке {r['всего_sku']}, из них с поисковым "
              f"трафиком {r['с_трафиком']}")

    for acc in ("oz_acc1", "oz_acc2"):
        print()
        print("=" * 78)
        print(f"ТОП-{TOP_N} ФРАЗ ПО ПОКАЗАМ — {acc} (за всю собранную глубину)")
        print("=" * 78)
        # unique_search_users — свойство самой фразы (сколько людей её искали), у всех
        # наших SKU по одной фразе оно одинаковое. Поэтому внутри недели берём max,
        # а не sum, иначе спрос раздувается в разы. unique_view_users — наоборот,
        # свои у каждого SKU, их суммируем: это наши показы по фразе.
        rows = db.query("""
            WITH per_week AS (
                SELECT period_start, query,
                       sum(unique_view_users)   AS показы,
                       max(unique_search_users) AS искали,
                       sum(order_count)         AS заказы,
                       sum(gmv)                 AS gmv,
                       count(DISTINCT sku)      AS sku,
                       min(NULLIF(position, 0)) AS поз
                FROM ozon_search_query WHERE account = %s
                GROUP BY period_start, query)
            SELECT query, sum(показы) AS показы, sum(искали) AS искали,
                   sum(заказы) AS заказы, round(sum(gmv)) AS gmv,
                   max(sku) AS sku, round(min(поз)::numeric, 0) AS луч_позиция
            FROM per_week GROUP BY query ORDER BY показы DESC LIMIT %s
        """, (acc, TOP_N))
        print(f"{'#':>3} {'фраза':<44}{'показы':>9}{'искали':>9}{'заказы':>8}"
              f"{'gmv':>11}{'SKU':>6}{'луч.поз':>9}")
        for i, r in enumerate(rows, 1):
            print(f"{i:>3} {(r['query'] or '')[:43]:<44}{r['показы'] or 0:>9}"
                  f"{r['искали'] or 0:>9}{r['заказы'] or 0:>8}"
                  f"{int(r['gmv'] or 0):>11}{r['sku']:>6}"
                  f"{str(r['луч_позиция'] or '—'):>9}")


if __name__ == "__main__":
    main()
