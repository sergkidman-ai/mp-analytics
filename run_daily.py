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
import collectors.ozon as oz              # noqa: E402
import collectors.ozon_postings as ozp    # noqa: E402
import reports.margin_by_sku as margin    # noqa: E402
import reports.margin_ozon_sku as ozm     # noqa: E402

ACCOUNTS = ["wb_acc1", "wb_acc2"]
OZON_ACCOUNTS = ["oz_acc1", "oz_acc2"]
PREMIUM_OZON = ["oz_acc1"]   # отзывы/звёздный рейтинг — только Премиум-Про (у Дисквэра доступа нет)


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
        step(f"WB воронка {acc}", lambda acc=acc: __import__("collectors.wb_funnel", fromlist=["main"]).main(acc))
        step(f"WB реклама {acc}", lambda acc=acc: __import__("collectors.wb_ads", fromlist=["main"]).main(acc))
        step(f"WB Джем позиции/запросы {acc}", lambda acc=acc: __import__("collectors.wb_jam", fromlist=["main"]).main(acc))
        for f, l in months:
            df, dt = f.isoformat(), l.isoformat()
            step(f"WB отчёт {acc} {df}..{dt}", lambda a=acc, x=df, y=dt: wb.main(a, x, y))
            step(f"Витрина маржи {acc} {df}", lambda a=acc, x=df, y=dt: margin.build(a, x, y))
    # --- Ozon: транзакции (по operation_date) + маржа по SKU (COGS из МС, org по аккаунту) ---
    for acc in OZON_ACCOUNTS:
        step(f"Ozon каталог {acc}", lambda a=acc: __import__("collectors.ozon_products", fromlist=["main"]).main(a))
        step(f"Ozon ФБО остатки {acc}", lambda a=acc: __import__("collectors.ozon_fbo_stock", fromlist=["main"]).main(a))
        step(f"Ozon реклама {acc}", lambda a=acc: __import__("collectors.ozon_ads", fromlist=["main"]).main(a))
        step(f"Ozon ставки {acc}", lambda a=acc: __import__("collectors.ozon_bids", fromlist=["main"]).main(a))
        if acc in PREMIUM_OZON:   # рейтинг недоступен без Премиум-Про
            step(f"Ozon отзывы/рейтинг {acc}", lambda a=acc: __import__("collectors.ozon_reviews", fromlist=["main"]).main(a))
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
