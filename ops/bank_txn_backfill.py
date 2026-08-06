# поток: inv
"""ops/bank_txn_backfill.py — добор банковской выписки в `bank_txn` за прошедшие дни.

Ежедневный прогон (`run_inv.py`) кладёт выписку в БД начиная с того дня, когда врезка появилась.
Этот скрипт добирает историю с 01.08.2026 (отсечка `OPEX_STMT_SINCE`) по обеим организациям:
Альфа — ООО «Цифровой Квадрат», Сбер — ООО «ДИСКВЭР».

Только ЧТЕНИЕ банка и запись в свою БД: в МойСклад ничего не пишет, платежей не создаёт.
Идемпотентно — повторный прогон даёт «новых 0» (натуральный ключ `(bank, nk)`).

    ./venv/bin/python ops/bank_txn_backfill.py                      # с отсечки по вчера
    ./venv/bin/python ops/bank_txn_backfill.py --from 2026-08-01 --to 2026-08-03
    ./venv/bin/python ops/bank_txn_backfill.py --bank alfa          # только один контур
"""
import datetime as dt
import os
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import collectors.alfa_statement as alfa              # noqa: E402
import collectors.alfa_ms as alfa_ms                  # noqa: E402  ORG_INN Цифрового Квадрата
import collectors.sber_statement as sber              # noqa: E402
import collectors.sber_ms as sber_ms                  # noqa: E402  ORG_INN Дисквэра
import collectors.bank_txn_store as txn_store         # noqa: E402


def _arg(argv, name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def alfa_accounts():
    """Боевые счета Альфы из .env (тот же выбор, что в run_inv.accounts)."""
    prod = (os.getenv("ALFA_ENV") or "sandbox").lower() != "sandbox"
    raw = (os.getenv("ALFA_ACCOUNT_PROD") if prod else os.getenv("ALFA_ACCOUNT")) or ""
    return [a.strip() for a in raw.split(",") if a.strip()]


def days(d_from, d_to):
    d0, d1 = dt.date.fromisoformat(d_from), dt.date.fromisoformat(d_to)
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def run_alfa(d_from, d_to):
    tot = {"seen": 0, "stored": 0, "dup": 0, "before_cutoff": 0, "ruled": 0}
    for account in alfa_accounts():
        for day in days(d_from, d_to):
            try:
                res = alfa.fetch_statement(account, day)
            except Exception as e:                            # noqa: BLE001
                print(f"  ✗ Альфа {account} {day}: {type(e).__name__}: {e}")
                continue
            st = txn_store.store(res["normalized"], "alfa", alfa_ms.ORG_INN,
                                 account=account, since=d_from,     # отсечка = начало запрошенного
                                 raws=res.get("transactions"))      # периода, иначе store режет по SINCE
            for k in tot:
                tot[k] += st[k]
    return tot


def run_sber(d_from, d_to):
    ops = sber.fetch_period(d_from, d_to)                     # сам идёт по дням и счетам
    return txn_store.store(ops, "sber", sber_ms.ORG_INN, since=d_from)


def main(argv):
    d_from = _arg(argv, "--from", txn_store.SINCE)
    d_to = _arg(argv, "--to", (dt.date.today() - dt.timedelta(days=1)).isoformat())
    if d_to < d_from:
        sys.exit(f"пустой период: {d_from}…{d_to}")
    only = (_arg(argv, "--bank") or "").lower()
    print(f"добор выписки в БД: {d_from}…{d_to}"
          + (f", только {only}" if only else ", обе организации"))

    for bank, fn, who in (("alfa", run_alfa, "Альфа / Цифровой Квадрат"),
                          ("sber", run_sber, "Сбер / Дисквэр")):
        if only and only != bank:
            continue
        try:
            st = fn(d_from, d_to)
        except Exception as e:                                # noqa: BLE001
            print(f"{who}: ОШИБКА {type(e).__name__}: {e}")
            continue
        print(f"{who}: операций {st['seen']}, новых {st['stored']}, уже было {st['dup']}, "
              f"до отсечки {st['before_cutoff']}, размечено правилом {st['ruled']}")

    rows = txn_store.db.query("""
        SELECT org_inn, count(*) n,
               coalesce(sum(amount) FILTER (WHERE direction='DEBIT'), 0)::float debit,
               coalesce(sum(amount) FILTER (WHERE direction='CREDIT'), 0)::float credit
        FROM bank_txn WHERE operation_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""",
        (d_from, d_to))
    print("--- в БД за период ---")
    for r in rows:
        print(f"  ИНН {r['org_inn']}: операций {r['n']}, "
              f"расход {r['debit']:,.2f} ₽, приход {r['credit']:,.2f} ₽".replace(",", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
