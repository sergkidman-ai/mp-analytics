# поток: cf
"""cf/run.py — ежедневный прогон раздела «Кэшфлоу»: остатки банков → автоплан → отчёт Маркета → витрина.

Cron (UTC): раз в день 05:20, после ночных выписок (inv 04:00) и run_daily (решение Сергея 22.09).
    ./venv/bin/python -m cf.run            # полный: остатки + план + витрина
    ./venv/bin/python -m cf.run --light    # только витрина
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cf import balances, plan_seed, build, yandex_netting, mp_balance  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", action="store_true")
    a = ap.parse_args()
    if not a.light:
        balances.main(["--days", "3"])   # 3 дня — догоняем пропуски, если банк лежал
        plan_seed.seed()
        try:
            yandex_netting.main()
        except Exception as e:  # отчёт Маркета не пришёл — прогноз возьмёт вчерашнюю таблицу
            print(f"[FAIL] Маркет united-netting: {type(e).__name__}: {str(e)[:200]}")
        try:
            mp_balance.main([])          # «Наши деньги на МП» — после Маркета: берёт его pending
        except Exception as e:           # площадка молчит — на главной останется вчерашний снимок
            print(f"[FAIL] Остатки лицевых счетов МП: {type(e).__name__}: {str(e)[:200]}")
    build.build(snapshot=None)


if __name__ == "__main__":
    main()
