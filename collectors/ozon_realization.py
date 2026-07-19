"""collectors/ozon_realization.py — «Отчёт о реализации товаров» Ozon → raw_ozon_realization.

POST /v2/finance/realization {month, year} — помесячный отчёт, который Ozon считает у себя.
Он единственный точно воспроизводит сплит строки «Продажи» из ЛК (проверено до рубля,
оба юрлица, июнь-2026):

  Выручка            = Σ delivery_commission.amount
  Баллы за скидки    = Σ delivery_commission.bonus
  Программы партнёров = Σ (bank_coinvestment + pick_up_point_coinvestment + stars)
  Продажи (итог)     = Выручка + Баллы + Партнёры

Постинги (financial_data.customer_price/price) этот сплит НЕ воспроизводят: на oz_acc1 совпало
случайно, на oz_acc2 расходилось на 19%. Источник сплита — только этот отчёт.

Запуск:  ./venv/bin/python collectors/ozon_realization.py [oz_acc1] [2026-01] [2026-06]
"""
import sys
import time
import pathlib
import datetime

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                       # noqa: E402
from collectors.ozon import _headers      # переиспользуем креды  # noqa: E402

REALIZATION_URL = "https://api-seller.ozon.ru/v2/finance/realization"


def fetch(account, year, month):
    """result отчёта о реализации за месяц (header+rows) или None.

    None, если отчёт ещё не сформирован: Ozon отдаёт его только после закрытия месяца,
    для текущего/будущего месяца → 404. Это не ошибка сбора — просто данных ещё нет.
    """
    H = _headers(account)
    for _ in range(6):
        r = requests.post(REALIZATION_URL, headers=H,
                          json={"month": month, "year": year}, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("result")
    raise RuntimeError(f"{account}: /v2/finance/realization 429 не отпустил")


def load_raw(account, year, month, result):
    if not result:
        return 0
    rec = {"account": account, "year": year, "month": month,
           "payload": psycopg2.extras.Json(result)}
    return db.upsert("raw_ozon_realization", [rec],
                     conflict_cols=["account", "year", "month"])


def _months(since, until):
    """Список (year, month) от since до until включительно (даты 'YYYY-MM')."""
    y, m = int(since[:4]), int(since[5:7])
    ey, em = int(until[:4]), int(until[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def collect(account="oz_acc1", since="2026-01", until=None):
    """Скачать отчёт о реализации за каждый месяц окна [since..until] для аккаунта."""
    until = until or datetime.date.today().strftime("%Y-%m")
    total = 0
    for y, m in _months(since, until):
        res = fetch(account, y, m)
        n = load_raw(account, y, m, res)
        rows = len((res or {}).get("rows") or [])
        print(f"  [oz realization] {account} {y}-{m:02d}: строк {rows}", flush=True)
        total += n
        time.sleep(0.4)
    return total


def sales_split(account, year, month):
    """{revenue, bonus, partners, total, rows} — сплит «Продажи» из сохранённого отчёта.

    account может быть списком аккаунтов или строкой; None/'' = оба юрлица Ozon.
    Возвращает None, если отчёт за месяц не собран.
    """
    if not account:
        accts = ["oz_acc1", "oz_acc2"]
    elif isinstance(account, (list, tuple)):
        accts = list(account)
    else:
        accts = [account]
    recs = db.query(
        """SELECT account, payload FROM raw_ozon_realization
           WHERE account = ANY(%s) AND year=%s AND month=%s""",
        (accts, year, month))
    # Требуем отчёт по КАЖДОМУ запрошенному аккаунту — иначе «Все юрлица» показали бы частичную
    # сумму как полную. Нет хотя бы одного → сплит не отдаём (блок в дашборде скрывается).
    got = {r["account"] for r in recs}
    if any(a not in got for a in accts):
        return None
    rev = bonus = partners = 0.0
    nrows = 0
    for rec in recs:
        for row in (rec["payload"].get("rows") or []):
            dc = row.get("delivery_commission") or {}
            rev += float(dc.get("amount") or 0)
            bonus += float(dc.get("bonus") or 0)
            partners += (float(dc.get("bank_coinvestment") or 0)
                         + float(dc.get("pick_up_point_coinvestment") or 0)
                         + float(dc.get("stars") or 0))
            nrows += 1
    return {"revenue": round(rev, 2), "bonus": round(bonus, 2),
            "partners": round(partners, 2), "total": round(rev + bonus + partners, 2),
            "rows": nrows}


def main(account="oz_acc1", since="2026-01", until=None):
    print(f"Ozon отчёт о реализации {account} с {since}", flush=True)
    n = collect(account, since, until)
    print(f"Итого месяцев → raw_ozon_realization: {n}", flush=True)


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "oz_acc1",
         a[2] if len(a) > 2 else "2026-01",
         a[3] if len(a) > 3 else None)
