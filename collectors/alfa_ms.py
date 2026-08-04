# поток: inv
"""collectors/alfa_ms.py — проводки выписки Альфа-Банка → МойСклад (paymentin/paymentout).

Организация — ООО «Цифровой Квадрат» (`ALFA_ORG_INN`, по умолчанию 7807355364).

Механика (антидубль, идемпотентность по `syncId`, гейт зарплат, контрагенты, статья расходов)
живёт в банконезависимом ядре `collectors/bank_ms.py` — вынесена туда 2026-08-02, когда рядом
появился контур Сбера (ООО «ДИСКВЭР», `collectors/sber_ms.py`). Здесь остаётся только то,
что про Альфу: откуда берём выписку, чья организация, и привязка платежей к приёмкам
(`alfa_link` написан под поставщиков Цифрового Квадрата).

БЕЗОПАСНОСТЬ: по умолчанию DRY-RUN (только чтение МС + план). `--apply` реально пишет в МС —
запускать ТОЛЬКО на настоящих выписках; данные песочницы фейковые, в боевой МС их не льём.

Запуск:
    ./venv/bin/python collectors/alfa_ms.py <accountNumber> [YYYY-MM-DD]           # dry-run
    ./venv/bin/python collectors/alfa_ms.py <accountNumber> [YYYY-MM-DD] --apply   # запись
"""
import os
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent           # каталог collectors/
BASE_DIR = HERE.parent
sys.path.insert(0, str(HERE))                            # соседи alfa_statement.py, bank_ms.py

import bank_ms                                           # noqa: E402  банконезависимое ядро
from bank_ms import (                                    # noqa: E402,F401  публичный API модуля
    skip_reason, find_existing, resolve_agent, get_opt, print_plan, print_stats,
    IGNORE_INN, _meta, _norm, _err_text, _ms_dt, _sync_id, _day_bounds,
)
from alfa_statement import fetch_statement               # noqa: E402  сосед по каталогу

ORG_INN = os.getenv("ALFA_ORG_INN", "7807355364")        # Цифровой Квадрат
EXPENSE_ITEM = os.getenv("ALFA_MS_EXPENSE_ITEM", "Закупка товаров")


# ── тонкие обёртки: сохраняют прежние сигнатуры вызовов из других модулей ─────────────────
def existing_index(typ, day, org_id=None):
    return bank_ms.existing_index(typ, day, org_id=org_id)


def resolve_org(inn=None):
    return bank_ms.resolve_org(inn or ORG_INN)


def resolve_expense_item(want=None):
    return bank_ms.resolve_expense_item(want or EXPENSE_ITEM)


def build_payment(op, org, agent, expense_item=None):
    return bank_ms.build_payment(op, org, agent, expense_item or EXPENSE_ITEM)


_link_supplies = bank_ms.link_supplies     # привязка платёж→приёмка переехала в общее ядро


def sync(normalized, apply=False):
    """Выписка Альфы → МС. Отсечка `ALFA_MS_SINCE`: операции раньше неё не пишем
    (до неё документы заводились руками)."""
    return bank_ms.sync(normalized, apply=apply, org_inn=ORG_INN,
                        expense_item=EXPENSE_ITEM,
                        since=os.getenv("ALFA_MS_SINCE") or None,
                        link_fn=_link_supplies)


def main(argv):
    apply = "--apply" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        sys.exit("usage: alfa_ms.py <accountNumber> [YYYY-MM-DD] [--apply]")
    account = pos[0]
    date = pos[1] if len(pos) > 1 else None
    res = fetch_statement(account, date)
    stats, plan = sync(res["normalized"], apply=apply)
    mode = "APPLY (запись в МС)" if apply else "DRY-RUN (только план)"
    print(f"[{mode}] счёт {account} дата {res['date']} — операций {len(res['normalized'])}")
    print_stats(stats)
    print_plan(plan)


if __name__ == "__main__":
    main(sys.argv[1:])
