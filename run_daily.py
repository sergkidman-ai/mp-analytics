"""run_daily.py — оркестратор сбора и пересборки витрин. Для cron 2×/день (МСК).

Обновляет: МойСклад (товары + себестоимость из report/stock), остатки WB (FBO),
пересобирает витрину маржи. Дашборд читает результат из Postgres.

Запуск:  ./venv/bin/python run_daily.py
TODO: скользящее окно WB-финотчёта по неделям (сейчас demo-период май фиксирован).
"""
import sys
import pathlib
import datetime
import traceback

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import collectors.moysklad as ms          # noqa: E402
import collectors.wb as wb                # noqa: E402
import reports.margin_by_sku as margin    # noqa: E402

PERIOD = ("wb_acc1", "2026-05-01", "2026-05-31")   # demo-период


def step(name, fn):
    t = datetime.datetime.now()
    try:
        fn()
        print(f"[ok] {name} ({(datetime.datetime.now()-t).seconds}с)", flush=True)
    except Exception:
        print(f"[FAIL] {name}:\n{traceback.format_exc()}", flush=True)


def main():
    t0 = datetime.datetime.now()
    print(f"[run_daily] старт {t0:%Y-%m-%d %H:%M}", flush=True)
    today = datetime.date.today().isoformat()
    step("МойСклад: товары + себестоимость", ms.main)
    step("WB: остатки (FBO)", lambda: wb.collect_stocks(PERIOD[0], today))
    step("Витрина маржи", lambda: margin.build(*PERIOD))
    print(f"[run_daily] готово за {(datetime.datetime.now()-t0).seconds}с", flush=True)


if __name__ == "__main__":
    main()
