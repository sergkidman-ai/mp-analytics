# поток: inv
"""invoice_bot/seed_payment_terms.py — условия оплаты поставщикам из файла заказчика в БД.

Источник: «Сроки оплаты по поставщикам» (Наталья, 2026-07-27) — колонки
  Поставщик | ИНН | Оплата | Дней | Размер предоплаты/оплаты по отсрочке | Момент формирования платежки

Маппинг на supplier_payment_terms (миграция 200 + 201):
  «предоплата» + «баланс менее X»      → prepayment_balance (advance_amount=размер, balance_threshold=X)
  «предоплата каждый счет отдельно»    → prepayment_per_order (платим счёт целиком)
  «отсрочка N» + «наступил срок оплаты»→ deferred (deferral_days=N, payment_cap=размер; None = вся сумма)

Скрипт идемпотентен (upsert по ИНН) и сам резолвит ms_agent_id: ИНН в МС НЕ уникален
(у Солюшнс принт две карточки — заказы идут только в «МСК»), поэтому при неоднозначности
берём карточку по подсказке имени, а если её нет — ту, где реально есть проведённые заказы.

Запуск:  ./venv/bin/python invoice_bot/seed_payment_terms.py [--dry]
"""
import os
import sys
import argparse
import urllib.parse
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
from ms import get, MS as MSU          # noqa: E402
from core import db                    # noqa: E402

# name_hint — только там, где в МС несколько карточек на один ИНН.
TERMS = [
    # (имя из файла, ИНН, метод, дней, кап платежа, аванс, порог, подсказка карточки МС)
    ("Колортек",              "7840480595", "prepayment_balance",   None, None,  50000, 20000, None),
    ("Солюшнс принт МСК",     "7806486149", "deferred",               14, 150000, None,  None, "МСК"),
    ("КПД",                   "7719482878", "deferred",                5, None,   None,  None, None),
    ("Одиссей",               "7730244274", "deferred",               14, 500000, None,  None, None),
    ("КВК ТРЕЙД",             "7722341813", "deferred",                5, 150000, None,  None, None),
    ("ТОНЕРОПТТОРГ",          "7725744338", "prepayment_balance",   None, None, 100000, 20000, None),
    ("Феррет",                "9731107362", "prepayment_per_order", None, None,   None,  None, None),
    ("Тонерстор",             "9717092410", "deferred",                3, 100000, None,  None, None),
    ("Компания РМ",           "7720494564", "deferred",               14, None,   None,  None, None),
    ("Позитив",               "7736123276", "prepayment_balance",   None, None, 100000, 20000, None),
    ("Картридж Трейд",        "9718075418", "deferred",                4, None,   None,  None, None),
]


def _orders_count(agent_id, days=90):
    since = date.today() - timedelta(days=days)
    flt = urllib.parse.quote(
        f"agent={MSU}/entity/counterparty/{agent_id};applicable=true;moment>={since:%Y-%m-%d} 00:00:00",
        safe="=;:")
    return len(get(f"/entity/purchaseorder?filter={flt}&limit=100").get("rows", []))


def resolve_agent(inn, name_hint=None):
    """→ (ms_agent_id, имя карточки МС). Неоднозначность решаем подсказкой, иначе — по заказам."""
    rows = get(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if not rows:
        return None, None
    if len(rows) == 1:
        return rows[0]["id"], rows[0]["name"]
    if name_hint:
        hit = [r for r in rows if name_hint.lower() in r["name"].lower()]
        if len(hit) == 1:
            return hit[0]["id"], hit[0]["name"]
    ranked = sorted(rows, key=lambda r: _orders_count(r["id"]), reverse=True)
    return ranked[0]["id"], ranked[0]["name"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="только показать, без записи в БД")
    a = ap.parse_args()

    payload, problems = [], []
    for name, inn, method, days, cap, adv, thr, hint in TERMS:
        agent_id, ms_name = resolve_agent(inn, hint)
        if not agent_id:
            problems.append(f"{inn} {name}: контрагент не найден в МС")
            continue
        payload.append({
            "inn": inn, "name": ms_name or name, "method": method,
            "deferral_days": days, "payment_cap": cap,
            "advance_amount": adv, "balance_threshold": thr,
            "ms_agent_id": agent_id, "active": True,
        })
        cap_s = f"{cap:.0f}₽" if cap else ("аванс %.0f₽ при <%.0f₽" % (adv, thr) if adv else "вся сумма")
        days_s = f"{days}д" if days else "—"
        print(f"{inn}  {method:22s} {days_s:4s} {cap_s:22s} → «{ms_name}»")

    if problems:
        print("\nПРОБЛЕМЫ:\n" + "\n".join(problems))
    if a.dry:
        print(f"\n[dry] {len(payload)} строк готовы, в БД НЕ записано")
        return
    db.upsert("supplier_payment_terms", payload, conflict_cols=["inn"],
              update_cols=["name", "method", "deferral_days", "payment_cap",
                           "advance_amount", "balance_threshold", "ms_agent_id", "active"])
    print(f"\nзаписано/обновлено: {len(payload)}")


if __name__ == "__main__":
    main()
