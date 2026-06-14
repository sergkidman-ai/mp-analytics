"""run_daily.py — оркестратор сбора и пересборки витрин (cron, МСК).

Скользящее окно: на каждом прогоне обновляет ТЕКУЩИЙ и ПРОШЛЫЙ месяц по ВСЕМ WB-аккаунтам
(финотчёт WB еженедельный, период пн–вс, выходит ~вторник — поэтому прогон по вт/ср даёт
свежие недельные деньги; прошлый месяц добираем из-за лага выкупа/возвратов).

Ключи периода всегда = первое..последнее число месяца (стабильны), даже если месяц неполный —
тогда строки за месяц просто перезаписываются на каждом прогоне.

Обновляет: МойСклад (товары + себест), карточки WB (раз в прогон), остатки FBO, витрину маржи.
Запуск:  ./venv/bin/python run_daily.py
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

ACCOUNTS = ["wb_acc1", "wb_acc2"]


def step(name, fn):
    t = datetime.datetime.now()
    try:
        fn()
        print(f"[ok] {name} ({(datetime.datetime.now()-t).seconds}с)", flush=True)
    except Exception:
        print(f"[FAIL] {name}:\n{traceback.format_exc()}", flush=True)


def _month_bounds(d):
    first = d.replace(day=1)
    nxt = (first + datetime.timedelta(days=32)).replace(day=1)
    last = nxt - datetime.timedelta(days=1)
    return first, last


def rolling_months(today):
    """[(first, last)] для текущего и прошлого месяца (полные границы)."""
    cur_first, cur_last = _month_bounds(today)
    prev_first, prev_last = _month_bounds(cur_first - datetime.timedelta(days=1))
    return [(prev_first, prev_last), (cur_first, cur_last)]


def main():
    t0 = datetime.datetime.now()
    print(f"[run_daily] старт {t0:%Y-%m-%d %H:%M}", flush=True)
    today = datetime.date.today()
    months = rolling_months(today)
    step("МойСклад: товары + себестоимость", ms.main)
    for acc in ACCOUNTS:
        step(f"WB карточки {acc}", lambda acc=acc: wb.collect_cards(acc))
        step(f"WB остатки FBO {acc}", lambda acc=acc: wb.collect_stocks(acc, today.isoformat()))
        for f, l in months:
            df, dt = f.isoformat(), l.isoformat()
            step(f"WB отчёт {acc} {df}..{dt}", lambda a=acc, x=df, y=dt: wb.main(a, x, y))
            step(f"Витрина маржи {acc} {df}", lambda a=acc, x=df, y=dt: margin.build(a, x, y))
    print(f"[run_daily] готово за {(datetime.datetime.now()-t0).seconds}с", flush=True)


if __name__ == "__main__":
    main()
