# поток: inv
"""collectors/sber_ms.py — проводки выписки Сбера → МойСклад (paymentin/paymentout).

Организация — ООО «ДИСКВЭР» (`SBER_ORG_INN`, по умолчанию 7811803918). Вся механика общая
с Альфой и живёт в `collectors/bank_ms.py`; здесь — только источник выписки
(`sber_statement`) и настройки контура Дисквэра.

Отличия от контура Альфы:
* **Привязка платежей к приёмкам ВКЛЮЧЕНА** (02.08.2026), тем же движком, что у Альфы —
  `bank_ms.link_supplies` → `alfa_link.link_payment`. Движок банконезависим и режет всё по
  НАШЕМУ юрлицу платежа (миграция 207): условия оплаты, черновики платёжек, приёмки и номера
  документов берутся внутри Дисквэра. Поставщики у двух фирм общие, приёмки — разные, поэтому
  разрез обязателен: без него платёж Дисквэра сел бы на приёмку Цифрового Квадрата.
  Первый источник состава — ЧЕРНОВИК платёжки (`payment_draft_queue`, мы его сами собирали),
  разбор назначения и аванс-FIFO — запасные пути.
* Отсечка своя — `SBER_MS_SINCE`: платежи Дисквэра в МС заводятся руками и уже лежат там
  (по 31.07.2026 включительно), повторно их не пишем. Антидубль (сумма+номер+день внутри
  ЭТОЙ организации) страхует и без отсечки.
* Выписка Сбера идёт **по одному дню на запрос**, счета берутся из `client-info`
  (у Дисквэра действующий один, 7 закрытых депозитов коллектор глотает сам).

БЕЗОПАСНОСТЬ: по умолчанию DRY-RUN — читаем банк и МС, считаем план, ничего не создаём.
Запись только с `--apply`.

Запуск:
    ./venv/bin/python collectors/sber_ms.py                          # за вчера, dry-run
    ./venv/bin/python collectors/sber_ms.py 2026-07-31               # за дату
    ./venv/bin/python collectors/sber_ms.py 2026-07-28 2026-07-31    # период
    ./venv/bin/python collectors/sber_ms.py 2026-07-31 --apply       # запись в МС
"""
import os
import sys
import pathlib
import datetime as dt

HERE = pathlib.Path(__file__).resolve().parent           # каталог collectors/
BASE_DIR = HERE.parent
CANON = pathlib.Path("/opt/mp-analytics")                # канонический чекаут (там .env)
sys.path.insert(0, str(HERE))                            # соседи bank_ms.py, sber_statement.py
sys.path.insert(0, str(BASE_DIR if (BASE_DIR / ".env").exists() else CANON))

from dotenv import load_dotenv                           # noqa: E402

_ENV = BASE_DIR / ".env"
load_dotenv(_ENV if _ENV.exists() else CANON / ".env")

import bank_ms                                           # noqa: E402  банконезависимое ядро
from collectors import sber_statement as ss              # noqa: E402

ORG_INN = os.getenv("SBER_ORG_INN", "7811803918")        # ООО «ДИСКВЭР»
EXPENSE_ITEM = os.getenv("SBER_MS_EXPENSE_ITEM", "Закупка товаров")
MSK = dt.timezone(dt.timedelta(hours=3))


def sync(normalized, apply=False):
    """Выписка Сбера → МС + привязка исходящих к приёмкам Дисквэра (см. докстринг модуля)."""
    return bank_ms.sync(normalized, apply=apply, org_inn=ORG_INN,
                        expense_item=EXPENSE_ITEM,
                        since=os.getenv("SBER_MS_SINCE") or None,
                        link_fn=bank_ms.link_supplies, link_in_fn=bank_ms.link_orders)


def run(date_from, date_to, accs=None, apply=False):
    """→ (операции, stats, plan). Выписка за период по счетам организации."""
    ops = ss.fetch_period(date_from, date_to, accs=accs)
    stats, plan = sync(ops, apply=apply)
    return ops, stats, plan


def main(argv):
    apply = "--apply" in argv
    acc = None
    if "--account" in argv:
        i = argv.index("--account")
        acc = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    dates = [a for a in argv if not a.startswith("--")]
    yday = (dt.datetime.now(MSK).date() - dt.timedelta(days=1)).isoformat()
    d_from = dates[0] if dates else yday
    d_to = dates[1] if len(dates) > 1 else d_from
    accs = [acc] if acc else None

    ops, stats, plan = run(d_from, d_to, accs=accs, apply=apply)
    mode = "APPLY (запись в МС)" if apply else "DRY-RUN (только план)"
    print(f"[{mode}] Сбер / ООО «ДИСКВЭР» ИНН {ORG_INN} — период {d_from}…{d_to}, "
          f"операций {len(ops)}")
    bank_ms.print_stats(stats)
    bank_ms.print_plan(plan)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
