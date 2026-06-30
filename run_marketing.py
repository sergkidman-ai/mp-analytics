"""run_marketing.py — оркестратор потока МАРКЕТИНГА (cron, МСК). Финансы → run_daily.py.

Поток «Маркетинг»: видимость/позиции (Джем), воронка, реклама/ДРР, отзывы/рейтинг.
НЕ трогает себест/маржу — маржу берёт из готовой витрины margin_by_sku (read-only).
Граница доменов — docs/BRIEF_MKT.md. Свои таблицы: wb_search_*, wb_funnel, wb_ads,
ad_spend_daily, ozon_ads, ozon_bids, ozon_rating, drops.

Запуск:  ./venv/bin/python run_marketing.py
"""
import sys
import pathlib
import datetime
import traceback

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

WB_ACCOUNTS = ["wb_acc1", "wb_acc2"]
OZON_ACCOUNTS = ["oz_acc1", "oz_acc2"]
PREMIUM_OZON = ["oz_acc1"]   # отзывы/звёздный рейтинг — только Премиум-Про (у Дисквэра нет доступа)


def step(name, fn):
    t = datetime.datetime.now()
    try:
        fn()
        print(f"[ok] {name} ({(datetime.datetime.now()-t).seconds}с)", flush=True)
    except Exception:
        print(f"[FAIL] {name}:\n{traceback.format_exc()}", flush=True)


def main():
    t0 = datetime.datetime.now()
    print(f"[run_marketing] старт {t0:%Y-%m-%d %H:%M}", flush=True)
    for acc in WB_ACCOUNTS:
        step(f"WB воронка {acc}", lambda a=acc: __import__("collectors.wb_funnel", fromlist=["main"]).main(a))
        step(f"WB реклама {acc}", lambda a=acc: __import__("collectors.wb_ads", fromlist=["main"]).main(a))
        step(f"WB Джем позиции/запросы {acc}", lambda a=acc: __import__("collectors.wb_jam", fromlist=["main"]).main(a))
    for acc in OZON_ACCOUNTS:
        step(f"Ozon реклама {acc}", lambda a=acc: __import__("collectors.ozon_ads", fromlist=["main"]).main(a))
        step(f"Ozon ставки {acc}", lambda a=acc: __import__("collectors.ozon_bids", fromlist=["main"]).main(a))
        if acc in PREMIUM_OZON:   # рейтинг недоступен без Премиум-Про
            step(f"Ozon отзывы/рейтинг {acc}", lambda a=acc: __import__("collectors.ozon_reviews", fromlist=["main"]).main(a))
    print(f"[run_marketing] готово за {(datetime.datetime.now()-t0).seconds}с", flush=True)


if __name__ == "__main__":
    main()
