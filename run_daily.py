"""run_daily.py — оркестратор ФИНАНСОВОГО потока (cron, МСК). См. поток МАРКЕТИНГА → run_marketing.py.

Поток «Финансы»: себест/COGS/маржа, выручка/расходы, P&L по площадкам. Витрину margin_by_sku
ПИШЕТ только этот поток (граница доменов — см. docs/BRIEF_FIN.md / docs/BRIEF_MKT.md).

Скользящее окно: на каждом прогоне обновляет ТЕКУЩИЙ и ПРОШЛЫЙ месяц по ВСЕМ аккаунтам
(финотчёт WB еженедельный, период пн–вс, выходит ~вторник; прошлый месяц добираем из-за лага
выкупа/возвратов). Ключи периода = первое..последнее число месяца (стабильны).

Обновляет: МойСклад (товары+себест, наборы, поставщики), карточки/остатки WB, себест отгрузок,
финотчёты WB→sales, Ozon каталог/остатки/транзакции/постинги, Яндекс; пересобирает витрины маржи.
Маркетинг (Джем/реклама/воронка/отзывы) сюда НЕ входит — он в run_marketing.py.
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
import collectors.ms_demand_cogs as msdc  # noqa: E402
import collectors.ozon as oz              # noqa: E402
import collectors.ozon_postings as ozp    # noqa: E402
import reports.margin_by_sku as margin    # noqa: E402
import reports.margin_ozon_sku as ozm     # noqa: E402

ACCOUNTS = ["wb_acc1", "wb_acc2"]
OZON_ACCOUNTS = ["oz_acc1", "oz_acc2"]


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
    step("Справочник товаров МС (закупочные/баркоды)", lambda: __import__("collectors.ms_products", fromlist=["main"]).main())
    step("Себест наборов (mix_data + МС)", lambda: __import__("collectors.set_cost", fromlist=["main"]).main())
    step("Поставщики/остатки МС", lambda: __import__("collectors.suppliers", fromlist=["main"]).main())
    step("Даты закупок (приёмки МС)", lambda: __import__("collectors.supplier_purchases", fromlist=["main"]).main())
    for acc in ACCOUNTS:
        step(f"WB карточки {acc}", lambda acc=acc: wb.collect_cards(acc))
        step(f"WB остатки FBO {acc}", lambda acc=acc: wb.collect_stocks(acc, today.isoformat()))
        for f, l in months:
            df, dt = f.isoformat(), l.isoformat()
            step(f"WB отчёт {acc} {df}..{dt}", lambda a=acc, x=df, y=dt: wb.main(a, x, y))
        # Себест отгрузок МС (report/stock/byoperation) — один раз на аккаунт после сбора
        # отчётов: идемпотентно/резюмируемо, тянет только НЕкэшированные отгрузки (новые/свежие).
        recent = (today - datetime.timedelta(days=80)).isoformat()
        step(f"Себест отгрузок МС {acc}", lambda a=acc, mf=recent: msdc.collect(a, moment_from=mf))
        for f, l in months:
            df, dt = f.isoformat(), l.isoformat()
            step(f"Витрина маржи {acc} {df}", lambda a=acc, x=df, y=dt: margin.build(a, x, y))
    # --- Ozon: транзакции (по operation_date) + маржа по SKU (COGS из МС, org по аккаунту) ---
    for acc in OZON_ACCOUNTS:
        step(f"Ozon каталог {acc}", lambda a=acc: __import__("collectors.ozon_products", fromlist=["main"]).main(a))
        step(f"Ozon ФБО остатки {acc}", lambda a=acc: __import__("collectors.ozon_fbo_stock", fromlist=["main"]).main(a))
        for f, l in months:
            df, dt = f.isoformat(), l.isoformat()
            step(f"Ozon транзакции {acc} {df}..{dt}", lambda a=acc, x=df, y=dt: oz.main(x, y, a))
            step(f"Ozon постинги {acc} {df}", lambda a=acc, x=df, y=dt: ozp.main(x, y, a))
            step(f"Ozon маржа {acc} {df}", lambda a=acc, x=df, y=dt: ozm.build(x, y, a))
    step("Яндекс.Маркет: заказы", lambda: __import__("collectors.yandex", fromlist=["main"]).main())
    step("Яндекс.Маркет: помесячно", lambda: __import__("collectors.yandex_monthly", fromlist=["main"]).main())
    print(f"[run_daily] готово за {(datetime.datetime.now()-t0).seconds}с", flush=True)


if __name__ == "__main__":
    main()
