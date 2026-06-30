"""collectors/supplier_purchases.py — даты последних закупок по поставщикам (МойСклад приёмки).

GET entity/supply?order=moment,desc&expand=agent — приёмки (закупки) от свежих к старым.
Копим по поставщику (agent.name): last_supply (первый встреченный = самый свежий) и
supply_count_90d. Идём страницами, пока moment не уйдёт за горизонт (по умолчанию 365 дней) —
дальше для «давно не закупались» уже неинтересно. Кладём в supplier_last_purchase.

Запуск:  ./venv/bin/python collectors/supplier_purchases.py
"""
import os
import sys
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS_API = "https://api.moysklad.ru/api/remap/1.2"
HORIZON_DAYS = 365


def _headers():
    tok = os.getenv("MOYSKLAD_TOKEN")
    if not tok:
        raise RuntimeError("MOYSKLAD_TOKEN не задан в .env")
    return {"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"}


def main():
    print("Закупки: даты последних приёмок по поставщикам", flush=True)
    H = _headers()
    today = datetime.date.today()
    horizon = today - datetime.timedelta(days=HORIZON_DAYS)
    last = {}          # supplier -> date (самая свежая приёмка)
    cnt90 = {}         # supplier -> число приёмок за 90 дней
    offset, limit, seen = 0, 100, 0   # МойСклад: при expand максимальный limit = 100
    stop = False
    while not stop:
        r = requests.get(f"{MS_API}/entity/supply", headers=H, timeout=120, params={
            "order": "moment,desc", "expand": "agent", "limit": limit, "offset": offset})
        r.raise_for_status()
        rows = r.json().get("rows", [])
        if not rows:
            break
        for row in rows:
            moment = (row.get("moment") or "")[:10]
            if not moment:
                continue
            d = datetime.date.fromisoformat(moment)
            if d < horizon:
                stop = True
                break
            name = ((row.get("agent") or {}).get("name") or "").strip()
            if not name:
                continue
            if name not in last:        # rows отсортированы desc → первый = самый свежий
                last[name] = d
            if d >= today - datetime.timedelta(days=90):
                cnt90[name] = cnt90.get(name, 0) + 1
        seen += len(rows)
        print(f"  обработано приёмок: {seen}, поставщиков: {len(last)}", flush=True)
        if len(rows) < limit:
            break
        offset += limit
    recs = [{"supplier": s, "last_supply": d.isoformat(), "supply_count_90d": cnt90.get(s, 0)}
            for s, d in last.items()]
    n = db.upsert("supplier_last_purchase", recs, conflict_cols=["supplier"],
                  update_cols=["last_supply", "supply_count_90d"])
    print(f"Записано поставщиков: {n} (горизонт {HORIZON_DAYS} дн)", flush=True)


if __name__ == "__main__":
    main()
