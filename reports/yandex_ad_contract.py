# поток: fin
"""reports/yandex_ad_contract.py — расходы на рекламу Маркета по ОТДЕЛЬНОМУ договору на размещение.

Зачем отдельно. Часть рекламы биллится не по основному договору кабинета, а по договору на
размещение (у нас — 354817/19, начисления март–июнь 2026). Партнёр-API этих сумм не отдаёт:
проверено 17.08.2026 — ни единый отчёт услуг (`united-marketplace-services`, 10 CSV), ни отчёт
взаиморасчётов (`united-netting`) за март-2026 суммы 4953,03 ₽ не содержат, номер договора в
сырье не встречается ни разу. Источник — ЛК/акты, ввод ручной.

Куда попадает: `yandex_ad_contract` → коллектор `yandex_monthly._write_finance` добавляет итог
месяца в строку «Продвижение» (`promotion`) и кладёт его же в `yandex_finance_monthly.ad_contract`,
откуда «Отчёты МП · Яндекс» рисует под-строку «в т.ч. реклама по договору».

Запуск:
    ./venv/bin/python -m reports.yandex_ad_contract list
    ./venv/bin/python -m reports.yandex_ad_contract set 2026-03 4953.03 [--contract 354817/19] [--note ...]
    ./venv/bin/python -m reports.yandex_ad_contract del 2026-03 [--contract 354817/19]
После правки — пересчёт витрины из сырья:
    ./venv/bin/python collectors/yandex_monthly.py 2025-11-01 --light
"""
import sys
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
ACCOUNT = "ya_acc1"
DEFAULT_CONTRACT = "354817/19"


def monthly(account=ACCOUNT):
    """{'YYYY-MM-01': сумма ₽} — итог по всем договорам за месяц."""
    return {str(r["month"]): float(r["s"] or 0) for r in db.query(
        """SELECT month, sum(amount) s FROM yandex_ad_contract
           WHERE account=%s GROUP BY 1""", (account,))}


def put(month, amount, contract=DEFAULT_CONTRACT, note=None, account=ACCOUNT):
    db.upsert("yandex_ad_contract",
              [{"account": account, "month": month, "contract": contract,
                "amount": round(float(amount), 2), "note": note}],
              conflict_cols=["account", "month", "contract"],
              update_cols=["amount", "note"])
    db.execute("""UPDATE yandex_ad_contract SET updated_at=now()
                  WHERE account=%s AND month=%s AND contract=%s""", (account, month, contract))


def drop(month, contract=DEFAULT_CONTRACT, account=ACCOUNT):
    db.execute("""DELETE FROM yandex_ad_contract
                  WHERE account=%s AND month=%s AND contract=%s""", (account, month, contract))


def _month(s):
    return s if len(s) == 10 else f"{s}-01"


def _arg(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "list"
    if cmd == "list":
        rows = db.query("""SELECT to_char(month,'YYYY-MM') m, contract, amount::float amount, note
                           FROM yandex_ad_contract WHERE account=%s ORDER BY 1,2""", (ACCOUNT,))
        for r in rows:
            amt = f"{r['amount']:>12,.2f}".replace(",", " ")
            print(f"  {r['m']} {r['contract']:<12} {amt} ₽  {r['note'] or ''}")
        print(f"итого {len(rows)} строк, {sum(r['amount'] for r in rows):,.2f} ₽".replace(",", " "))
    elif cmd == "set" and len(a) >= 3:
        put(_month(a[1]), a[2], _arg(a, "--contract", DEFAULT_CONTRACT), _arg(a, "--note"))
        print(f"записано: {a[1]} {a[2]} ₽")
    elif cmd == "del" and len(a) >= 2:
        drop(_month(a[1]), _arg(a, "--contract", DEFAULT_CONTRACT))
        print(f"удалено: {a[1]}")
    else:
        print(__doc__)
