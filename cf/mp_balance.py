# поток: cf
"""cf/mp_balance.py — «Наши деньги на МП»: остаток лицевого счёта каждой площадки → cf_mp_balance.

Это заработанное, что площадка ещё держит у себя. Берём ОФИЦИАЛЬНУЮ цифру площадки, а не
разность «начислено − выплачено»: у накопительного метода нет входящего остатка дебиторки,
а выписки Озон Банка с дырами (проверка 22.09.2026 давала расхождение до 2,8 млн ₽).

Источники (все бесплатные, только чтение):
- Ozon   `POST /v1/finance/cash-flow-statement/list` (with_details) → `details[].end_balance_amount`
         последнего периода. Сверено: начисление недели = end − begin + payment, совпало с нашим
         расчётом по начислениям до рубля (07–13.09 и 14–20.09.2026).
- WB     `GET finance-api.wildberries.ru/api/v1/account/balance` → `current`. Нужен скоуп
         «Финансы»: из 10 наших токенов его имеет ТОЛЬКО `WB_TOKEN_PRICES_ACC1` (Цифровой).
         У Дисквэра такого токена нет — строки по wb_acc2 не будет, пока его не заведут.
- Яндекс уже в БД: `cf_ya_netting_day.pending` (собирает cf/yandex_netting.py, сверено с ЛК).

Запуск:  ./venv/bin/python -m cf.mp_balance [--day YYYY-MM-DD]
"""
import os
import sys
import time
import argparse
import pathlib
import datetime as dt

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import _headers as _oz_headers  # noqa: E402
from cf.mp_forecast import ACC_ORG  # noqa: E402

load_dotenv(BASE_DIR / ".env")

OZON_CF_URL = "https://api-seller.ozon.ru/v1/finance/cash-flow-statement/list"
WB_BALANCE_URL = "https://finance-api.wildberries.ru/api/v1/account/balance"
WB_FINANCE_TOKEN = {"wb_acc1": "WB_TOKEN_PRICES_ACC1"}   # скоуп «Финансы» есть только здесь


def ozon_balance(account, day):
    """Остаток лицевого счёта Ozon = end_balance_amount последнего периода отчёта о движении."""
    body = {"date": {"from": (day - dt.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00.000Z"),
                     "to": day.strftime("%Y-%m-%dT00:00:00.000Z")},
            "page": 1, "page_size": 10, "with_details": True}
    r = requests.post(OZON_CF_URL, headers=_oz_headers(account), json=body, timeout=60)
    r.raise_for_status()
    det = (r.json().get("result") or {}).get("details") or []
    if not det:
        raise RuntimeError(f"Ozon {account}: пустой details за 30 дней")
    last = max(det, key=lambda d: d["period"]["end"])
    return float(last["end_balance_amount"]), f"ozon cash-flow-statement {last['period']['end'][:10]}"


def wb_balance(account):
    """Баланс ЛК WB. Лимит ~1 запрос/мин — зовём раз в сутки."""
    env = WB_FINANCE_TOKEN.get(account)
    token = os.getenv(env) if env else None
    if not token:
        return None, f"нет токена со скоупом «Финансы» ({env or '—'})"
    for _ in range(3):      # лимит ~1 запрос/мин на весь аккаунт — ждём и пробуем ещё раз
        r = requests.get(WB_BALANCE_URL, headers={"Authorization": token}, timeout=60)
        if r.status_code != 429:
            break
        time.sleep(int(r.headers.get("X-Ratelimit-Retry", r.headers.get("Retry-After", "20"))) + 5)
    if r.status_code == 403:
        return None, "403: у токена нет скоупа «Финансы»"
    if r.status_code == 429:
        return None, "429: лимит 1 запрос/мин не отпустил"
    r.raise_for_status()
    return float(r.json()["current"]), "wb account/balance"


def yandex_balance(account):
    """Ждёт выплаты по Маркету — из витрины взаимозачёта (её пополняет cf/yandex_netting.py)."""
    row = db.query("""select coalesce(sum(pending), 0)::float p, max(day) d, max(loaded_at) l
                        from cf_ya_netting_day where account = %s""", (account,))[0]
    if row["d"] is None:
        return None, "cf_ya_netting_day пуста"
    return row["p"], f"cf_ya_netting_day по {row['d']:%d.%m} (сбор {row['l']:%d.%m %H:%M})"


def collect(day=None):
    day = day or dt.date.today()
    rows, skipped = [], []
    for account in sorted(ACC_ORG):
        platform = {"w": "wb", "o": "ozon", "y": "yandex"}[account[0]]
        try:
            if platform == "ozon":
                bal, src = ozon_balance(account, day)
            elif platform == "wb":
                bal, src = wb_balance(account)
            else:
                bal, src = yandex_balance(account)
        except Exception as e:                      # площадка молчит — не роняем весь прогон
            bal, src = None, f"{type(e).__name__}: {str(e)[:120]}"
        if bal is None:
            skipped.append(f"{account}: {src}")
            continue
        rows.append({"account": account, "day": day, "platform": platform,
                     "org_inn": ACC_ORG[account], "balance": round(bal, 2), "source": src})
    if rows:
        db.upsert("cf_mp_balance", rows, conflict_cols=["account", "day"])
    return rows, skipped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.day) if a.day else dt.date.today()
    rows, skipped = collect(day)
    print(f"Наши деньги на МП {day:%d.%m.%Y}: {sum(r['balance'] for r in rows):,.2f} ₽", flush=True)
    for r in rows:
        print(f"  {r['account']:<8} {r['balance']:>14,.2f}  ({r['source']})", flush=True)
    for s in skipped:
        print(f"  [нет цифры] {s}", flush=True)


if __name__ == "__main__":
    main()
