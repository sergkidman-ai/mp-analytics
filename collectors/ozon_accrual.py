"""collectors/ozon_accrual.py — начисления Ozon по дням → raw_ozon_accrual.

Источник: POST /v1/finance/accrual/by-day (один день за запрос, пагинация last_id).
Замена /v3/finance/transaction/list, который Ozon отключил 09.09.2026 (code 9 «obsolete
method»). Старый collectors/ozon.py и raw_ozon_transaction НЕ трогаем — это история до 08.09.

- Сырьё целиком в payload JSONB, UPSERT по account+accrual_id; разбор по type_id — в витрине.
- last_id — СТРОКА: на первой странице поле не передаём, пустой/"0" в ответе = конец.
- total_amount приходит объектом {"amount": "-88.27", "currency": "RUB"}.
- Поток данных тот же, что у транзакций: 05.09 oz_acc1 — 125 записей / 135 089 ₽ в обоих.

Режим сверки (--check): по каждому дню сравнить число записей и сумму со старой
raw_ozon_transaction (operation_date = дню). Работает только для дней до 08.09.

Запуск:  ./venv/bin/python collectors/ozon_accrual.py 2026-09-01 2026-09-21 [oz_acc1] [--check]
"""
import sys
import time
import pathlib
import datetime as dt

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import _headers  # noqa: E402

BYDAY_URL = "https://api-seller.ozon.ru/v1/finance/accrual/by-day"


def _amount(a):
    v = a.get("total_amount") or 0
    if isinstance(v, dict):
        v = v.get("amount") or 0
    return float(v)


def fetch_day(account, day):
    """Все начисления за один день. Обрабатываем 429."""
    H = _headers(account)
    out, last_id = [], ""
    while True:
        body = {"date": day, "limit": 1000}
        if last_id:
            body["last_id"] = last_id
        r = requests.post(BYDAY_URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"by-day {account} {day}: {r.status_code} {r.text[:200]}")
        res = r.json().get("result") or r.json()
        out.extend(res.get("accruals") or [])
        nxt = str(res.get("last_id") or "")
        if nxt in ("", "0") or nxt == last_id:
            return out
        last_id = nxt
        time.sleep(0.3)


def load_raw(account, recs):
    rows = [{"account": account, "accrual_id": int(a["accrual_id"]),
             "accrual_date": a.get("date"), "accrued_category": a.get("accrued_category"),
             "unit_number": a.get("unit_number"), "total_amount": round(_amount(a), 2),
             "payload": psycopg2.extras.Json(a)}
            for a in recs if a.get("accrual_id") is not None]
    if not rows:
        return 0
    return db.upsert("raw_ozon_accrual", rows, conflict_cols=["account", "accrual_id"])


def check_day(account, day, recs):
    """Сверка дня со старой raw_ozon_transaction: (ok, строка для лога)."""
    old = db.query(
        """select count(*) n, coalesce(sum((payload->>'amount')::numeric), 0) s
             from raw_ozon_transaction
            where account = %s and (payload->>'operation_date')::date = %s""",
        (account, day))[0]
    n_new, s_new = len(recs), sum(_amount(a) for a in recs)
    ok = old["n"] == n_new and abs(float(old["s"]) - s_new) < 0.5
    return ok, (f"  [check] {day} new {n_new} / {s_new:,.2f}  old {old['n']} / "
                f"{float(old['s']):,.2f}  {'OK' if ok else 'РАСХОЖДЕНИЕ'}")


def _days(date_from, date_to):
    d, end = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    while d <= end:
        yield d.isoformat()
        d += dt.timedelta(days=1)


def main(date_from, date_to, account="oz_acc1", check=False):
    print(f"Ozon начисления {account} {date_from}..{date_to}", flush=True)
    n_tot, s_tot, bad = 0, 0.0, []
    for day in _days(date_from, date_to):
        recs = fetch_day(account, day)
        n_raw = load_raw(account, recs)
        s = sum(_amount(a) for a in recs)
        n_tot, s_tot = n_tot + len(recs), s_tot + s
        print(f"  [oz accrual] {day} записей {len(recs)} → raw {n_raw}, итог {s:,.2f}", flush=True)
        if check:
            ok, line = check_day(account, day, recs)
            print(line, flush=True)
            if not ok:
                bad.append(day)
    print(f"Итого: записей {n_tot}, сумма {s_tot:,.2f}", flush=True)
    if check and bad:
        raise RuntimeError(f"сверка не сошлась: {', '.join(bad)}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    main(a[0], a[1] if len(a) > 1 else a[0], a[2] if len(a) > 2 else "oz_acc1",
         check="--check" in sys.argv)
