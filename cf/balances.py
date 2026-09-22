# поток: cf
"""cf/balances.py — остатки на расчётных счетах на конец дня → cf_balance.

Альфа (Цифровой) и Сбер (Дисквэр) отдают сводку выписки за дату:
    Альфа  GET {ALFA_API_BASE}/jp/v1/statement/summary?accountNumber=&statementDate=
    Сбер   GET /fintech/api/v2/statement/summary?accountNumber=&statementDate=
Берём closingBalance — остаток на конец statementDate. Авторизация и mTLS — из коллекторов
потока inv (alfa_statement._session, sber_auth.api), их не меняем.

Озон Банк API не имеет: опорный остаток вводится вручную (source=manual) со страницы
«Кэшфлоу», дальше cf/build.py докатывает его операциями из bank_txn.

Запуск:
    ./venv/bin/python -m cf.balances            # вчера
    ./venv/bin/python -m cf.balances --days 70  # история остатков за 70 дней
"""
import argparse
import datetime as dt
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402

ORG_CIFR = "7807355364"
ORG_DISK = "7811803918"


def _amount(obj):
    if isinstance(obj, dict):
        obj = obj.get("amount")
    return None if obj is None else round(float(obj), 2)


def alfa_accounts():
    raw = os.getenv("ALFA_ACCOUNT_PROD") or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def fetch_alfa(dates):
    from collectors import alfa_statement as A
    cfg = A._cfg()
    s = A._session(cfg)
    out = []
    for acc in alfa_accounts():
        for d in dates:
            r = s.get(cfg["base"] + "/jp/v1/statement/summary",
                      params={"accountNumber": acc, "statementDate": d.isoformat()}, timeout=60)
            if r.status_code != 200:
                print(f"  [warn] alfa …{acc[-4:]} {d}: HTTP {r.status_code}")
                continue
            bal = _amount(r.json().get("closingBalanceRub") or r.json().get("closingBalance"))
            if bal is not None:
                out.append(dict(bank="alfa", account=acc, org_inn=ORG_CIFR, bal_date=d,
                                balance=bal, source="api"))
    return out


def fetch_sber(dates):
    from collectors import sber_statement as S
    from collectors import sber_auth as sa
    out = []
    for a in S.accounts():
        acc = a["number"]
        for d in dates:
            r = sa.api("GET", "/fintech/api/v2/statement/summary",
                       params={"accountNumber": acc, "statementDate": d.isoformat()}, timeout=60)
            if r.status_code != 200:
                print(f"  [warn] sber …{acc[-4:]} {d}: HTTP {r.status_code}")
                continue
            bal = _amount(r.json().get("closingBalanceRub") or r.json().get("closingBalance"))
            if bal is not None:
                out.append(dict(bank="sber", account=acc, org_inn=ORG_DISK, bal_date=d,
                                balance=bal, source="api"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="сколько дней назад, начиная со вчера")
    args = ap.parse_args(argv)
    today = dt.date.today()
    dates = [today - dt.timedelta(days=i) for i in range(1, args.days + 1)]
    total = 0
    for name, fn in (("alfa", fetch_alfa), ("sber", fetch_sber)):
        try:
            rows = fn(dates)
        except Exception as e:  # один банк упал — второй всё равно пишем
            print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        db.upsert("cf_balance", rows, ["bank", "account", "bal_date"])
        last = max(rows, key=lambda r: r["bal_date"]) if rows else None
        print(f"[ok] {name}: {len(rows)} остатков" +
              (f", на {last['bal_date']}: " + f"{last['balance']:,.0f}".replace(",", " ") + " ₽" if last else ""))
        total += len(rows)
    return total


if __name__ == "__main__":
    main()
