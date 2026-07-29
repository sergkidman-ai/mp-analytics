# поток: inv
"""detect_vat_rate.py — ставка НДС поставщика из НДС-оговорки его последних платежей.

Читает выписку Альфа-Банка за последние N дней, берёт исходящие платежи поставщикам из
`supplier_payment_terms` и достаёт из назначения ставку: «В том числе НДС 22%, 9016.39 руб.»
→ 22, «НДС не облагается» / «Без НДС» → 0.

Почему выписка, а не заказы МС: назначение писал человек, который знает режим поставщика, и
это ровно тот текст, который увидит его бухгалтерия. Заказ МС даёт `vatEnabled`, но у аванса
заказа нет вообще, а ставка нужна и там.

РАЗНОБОЙ НЕ РЕШАЕМ АВТОМАТОМ. Если у поставщика в разных платежах разные ставки — строка
помечается «спорно» и НЕ пишется: ставка не та величина, которую можно угадать голосованием.
Такие проверяются глазами и ставятся руками в таблице условий оплаты на дашборде.

Значение — предложение машины, а не истина: колонка редактируется в дашборде
(«Условия оплаты поставщикам»), последнее слово за владельцем.

Запуск:
    ./venv/bin/python invoice_bot/detect_vat_rate.py             # dry-run, 90 дней
    ./venv/bin/python invoice_bot/detect_vat_rate.py --days 180
    ./venv/bin/python invoice_bot/detect_vat_rate.py --apply     # записать в БД
"""
import re
import sys
import pathlib
import datetime as dt
import collections

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else "/opt/mp-analytics/.env")

import core.db as db                                             # noqa: E402
import collectors.alfa_statement as alfa                         # noqa: E402
import run_inv                                                   # noqa: E402

MSK = dt.timezone(dt.timedelta(hours=3))
RATE = re.compile(r"(?i)НДС[\s,-]*(\d{1,2})\s*%")
NO_VAT = re.compile(r"(?i)НДС\s+не\s+облагается|без\s+НДС")


def rate_of(purpose):
    """Ставка из назначения платежа: число / 0 («не облагается») / None (оговорки нет)."""
    m = RATE.search(purpose or "")
    if m:
        return int(m.group(1))
    return 0 if NO_VAT.search(purpose or "") else None


def collect(days):
    """→ {ИНН: Counter(ставка: сколько платежей)} по исходящим платежам поставщикам."""
    known = {r["inn"] for r in db.query("SELECT inn FROM supplier_payment_terms")}
    seen = collections.defaultdict(collections.Counter)
    today = dt.datetime.now(MSK).date()
    for account in run_inv.accounts():
        for k in range(days):
            day = (today - dt.timedelta(days=k)).isoformat()
            try:
                ops = alfa.fetch_statement(account, day)["normalized"]
            except Exception as e:                    # день без выписки не повод падать
                print(f"  ! {account} {day}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            for op in ops:
                inn = (op.get("counterparty_inn") or "").strip()
                if op.get("direction") != "DEBIT" or inn not in known:
                    continue
                r = rate_of(op.get("purpose"))
                if r is not None:
                    seen[inn][r] += 1
    return seen


def main(argv):
    apply = "--apply" in argv
    days = 90
    for i, a in enumerate(argv):
        if a == "--days" and i + 1 < len(argv):
            days = int(argv[i + 1])
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1])

    seen = collect(days)
    rows = db.query("SELECT inn, name, vat_rate FROM supplier_payment_terms ORDER BY name")
    print(f"выписка за {days} дн. — {'ЗАПИСЬ' if apply else 'DRY-RUN'}\n")
    upd = 0
    for r in rows:
        c = seen.get(r["inn"])
        if not c:
            print(f"  · {r['name'][:30]:30} — платежей с НДС-оговоркой не нашёл, оставляю "
                  f"{r['vat_rate'] if r['vat_rate'] is not None else 'пусто'}")
            continue
        if len(c) > 1:                                # ставка не голосуется — только руками
            print(f"  ⚠ {r['name'][:30]:30} СПОРНО {dict(c)} — не пишу, проверить глазами")
            continue
        rate = next(iter(c))
        label = "без НДС" if rate == 0 else f"{rate}%"
        if r["vat_rate"] == rate:
            print(f"  = {r['name'][:30]:30} {label} (уже стоит, платежей {c[rate]})")
            continue
        print(f"  {'✓' if apply else '•'} {r['name'][:30]:30} {label} "
              f"(платежей {c[rate]}, было {r['vat_rate'] if r['vat_rate'] is not None else 'пусто'})")
        if apply:
            db.execute("UPDATE supplier_payment_terms SET vat_rate=%s, updated_at=now() WHERE inn=%s",
                       (rate, r["inn"]))
        upd += 1
    print(f"\n{'обновлено' if apply else 'обновилось бы'}: {upd} из {len(rows)}")
    if not apply and upd:
        print("записать: --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
