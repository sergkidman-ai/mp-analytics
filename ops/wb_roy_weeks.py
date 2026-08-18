#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_weeks.py — воронка ВБ за ДВЕ недели по каждой карточке, вход для Роя.

`wb_funnel` в базе лежит помесячно, а Рой сравнивает неделю с неделей — поэтому недельные
срезы берём прямо из sales-funnel v3 и складываем в json рядом с отчётами. В базу не пишем:
это не новая сущность, а вход одного отчёта.

Недели — всегда полные пн-вс, чтобы дни недели совпадали (сравнение блоками одинаковых дней:
правило замеров с 13.08.2026). Окно Джема (`wb_search_report.period_start`) совпадает с началом
отчётной недели день в день.

Запуск:
  ./venv/bin/python -m ops.wb_roy_weeks                  # последняя закрытая неделя
  ./venv/bin/python -m ops.wb_roy_weeks --end 2026-08-16 # конкретное воскресенье
"""
import os
import sys
import json
import time
import argparse
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")
URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
OUT_DIR = BASE_DIR / "docs" / "reports"
LIM = 1000


def last_sunday(today=None):
    """Последнее закрытое воскресенье. В понедельник это вчера — неделя целиком в прошлом."""
    d = today or datetime.date.today()
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7 or 7)


def weeks(end):
    w2 = (end - datetime.timedelta(days=6), end)
    w1 = (w2[0] - datetime.timedelta(days=7), w2[0] - datetime.timedelta(days=1))
    return w1, w2


def fetch(account, start, end):
    """Все карточки с трафиком за период. Пагинация limit/offset — параметр page WB игнорирует."""
    tok = os.getenv(TOKEN_ENV[account])
    if not tok:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан в .env")
    H = {"Authorization": tok, "Content-Type": "application/json"}
    out, offset = {}, 0
    while True:
        body = {"nmIDs": [], "brandNames": [], "subjectIDs": [], "tagIDs": [],
                "selectedPeriod": {"start": start, "end": end},
                "orderBy": {"field": "openCard", "mode": "desc"}, "limit": LIM, "offset": offset}
        r = requests.post(URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
            continue
        r.raise_for_status()
        prods = (r.json().get("data") or {}).get("products", [])
        for p in prods:
            sel = (p.get("statistic") or {}).get("selected") or {}
            nm = (p.get("product") or {}).get("nmId")
            if not nm:
                continue
            out[nm] = {"o": int(sel.get("openCount") or 0), "c": int(sel.get("cartCount") or 0),
                       "ord": int(sel.get("orderCount") or 0), "sum": float(sel.get("orderSum") or 0)}
        print(f"  [{start}..{end}] offset {offset}: +{len(prods)} (всего {len(out)})", flush=True)
        if len(prods) < LIM:
            break
        # Стоп только когда ВСЯ порция пуста: сортировка идёт по openCard, а заказ бывает и у
        # карточки с нулём открытий (карусель, повтор из корзины) — по последней строке судить нельзя.
        page_live = sum((((p.get("statistic") or {}).get("selected") or {}).get("orderCount") or 0)
                        + (((p.get("statistic") or {}).get("selected") or {}).get("openCount") or 0)
                        for p in prods)
        if not page_live:
            print("  порция целиком без трафика и заказов — стоп", flush=True)
            break
        offset += LIM
        if offset >= 8000:            # предохранитель: дальше только нулевой хвост 14k карточек
            print("  дошли до 8000 — дальше нулевой хвост, стоп", flush=True)
            break
        time.sleep(3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='wb_acc1')
    ap.add_argument('--end', default='', help='воскресенье отчётной недели (по умолчанию последнее)')
    a = ap.parse_args()

    end = datetime.date.fromisoformat(a.end) if a.end else last_sunday()
    if end.weekday() != 6:
        sys.exit(f"--end должен быть воскресеньем, а {end} — это {end.strftime('%A')}")
    w1, w2 = weeks(end)
    print(f"Рой: неделя {w2[0]}..{w2[1]} против {w1[0]}..{w1[1]}", flush=True)

    data = {"w1": {str(k): v for k, v in fetch(a.account, w1[0].isoformat(), w1[1].isoformat()).items()},
            "w2": {str(k): v for k, v in fetch(a.account, w2[0].isoformat(), w2[1].isoformat()).items()},
            "meta": {"w1": [w1[0].isoformat(), w1[1].isoformat()],
                     "w2": [w2[0].isoformat(), w2[1].isoformat()], "account": a.account}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sfx = '' if a.account == 'wb_acc1' else f'_{a.account}'
    out = OUT_DIR / f"mkt_roy_funnel_{end}{sfx}.json"
    out.write_text(json.dumps(data, ensure_ascii=False))
    print(f"w1 {len(data['w1'])} nm · w2 {len(data['w2'])} nm → {out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
