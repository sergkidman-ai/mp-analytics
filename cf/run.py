# поток: cf
"""cf/run.py — ежедневный прогон раздела «Кэшфлоу»: остатки банков → автоплан → витрина.

Cron (UTC): после ночных выписок (inv 04:00) и run_daily; днём — пересборка витрины,
чтобы подтягивались новые операции банка и статусы оплат поставщикам.
    ./venv/bin/python -m cf.run            # полный: остатки + план + витрина
    ./venv/bin/python -m cf.run --light    # только витрина
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cf import balances, plan_seed, build  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", action="store_true")
    a = ap.parse_args()
    if not a.light:
        balances.main(["--days", "3"])   # 3 дня — догоняем пропуски, если банк лежал
        plan_seed.seed()
    build.build(snapshot=None)


if __name__ == "__main__":
    main()
