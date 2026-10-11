# поток: fin
"""Weekly alert about newly observed marketplace charges and service names.

Reads existing raw finance data only. First run seeds the historical baseline; later
runs send only unseen types to Natalia. --send delivers Telegram, default is preview.
"""
import argparse
import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

from core import db
from collectors.ozon_accrual_adapter import _fees
from ops.wb_lines_registry import norm as wb_norm

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
NATALIA_ID = os.getenv("TG_PRC_NATALIA_ID", "1231747786").strip()
ACCOUNT = {"oz_acc1": "Цифровой", "oz_acc2": "Дисквэр",
           "wb_acc1": "Цифровой", "wb_acc2": "Дисквэр", "ya_acc1": "Цифровой"}
PLATFORM = {"ozon": "Ozon", "wb": "ВБ", "yandex": "Маркет"}
TYPES = {str(x["id"]): x["description"] for x in json.loads(
    (ROOT / "docs/reference/ozon_accrual_types.json").read_text())["accrual_types"]}


def _money(value):
    return Decimal(str(value or 0))


def _add(seen, platform, kind, code, label, account, day, amount):
    key = (platform, kind, code)
    if key not in seen:
        seen[key] = {"label": label.strip() or code, "first": day, "last": day,
                     "accounts": set(), "amount": Decimal(0), "rows": 0}
    item = seen[key]
    item["first"] = min(item["first"], day)
    item["last"] = max(item["last"], day)
    item["accounts"].add(ACCOUNT.get(account, account))
    item["amount"] += amount
    item["rows"] += 1


def scan_ozon(seen):
    rows = db.query("SELECT account, accrual_date, payload FROM raw_ozon_accrual")
    for row in rows:
        for type_id, amount in _fees(row["payload"]):
            if type_id is not None and amount < 0:
                code = str(type_id)
                _add(seen, "ozon", "начисление", code, TYPES.get(code, f"Тип {code}"),
                     row["account"], row["accrual_date"], -_money(amount))


def scan_wb(seen):
    rows = db.query("""SELECT account, (payload->>'create_dt')::date AS day,
                             payload->>'supplier_oper_name' AS op,
                             payload->>'bonus_type_name' AS bonus,
                             payload->>'return_amount' AS return_amount,
                             payload->>'delivery_rub' AS delivery,
                             payload->>'storage_fee' AS storage,
                             payload->>'acceptance' AS acceptance,
                             payload->>'cashback_amount' AS cashback,
                             payload->>'penalty' AS penalty,
                             payload->>'deduction' AS deduction
                      FROM raw_wb_report
                      WHERE coalesce((payload->>'deduction')::numeric,0)>0
                         OR coalesce((payload->>'penalty')::numeric,0)>0
                         OR coalesce((payload->>'delivery_rub')::numeric,0)>0
                         OR coalesce((payload->>'storage_fee')::numeric,0)>0
                         OR coalesce((payload->>'acceptance')::numeric,0)>0
                         OR coalesce((payload->>'cashback_amount')::numeric,0)>0""")
    for r in rows:
        if not r["day"]:
            continue
        for field, kind in (("deduction", "удержание"), ("penalty", "штраф"),
                            ("delivery", "логистика"), ("storage", "хранение"),
                            ("acceptance", "приёмка"), ("cashback", "баллы")):
            amount = _money(r[field])
            if amount <= 0:
                continue
            name = wb_norm((r["bonus"] if field in ("deduction", "penalty") else r["op"]) or r["op"] or kind)
            if field == "delivery" and _money(r["return_amount"]) > 0:
                name += " (обратная)"
            _add(seen, "wb", kind, name, name, r["account"], r["day"], amount)


def scan_yandex(seen):
    rows = db.query("""SELECT account, svc_date, service, category, cost, accrued,
                             payload->>'service_name' AS name
                      FROM raw_yandex_services
                      WHERE cost > 0 OR accrued > 0""")
    for r in rows:
        if not r["svc_date"]:
            continue
        name = " ".join((r["name"] or "").split())
        code = r["service"] + "|" + name
        label = name or r["service"]
        _add(seen, "yandex", "услуга", code, label, r["account"], r["svc_date"],
             _money(r["cost"]))


def scan():
    seen = {}
    scan_ozon(seen)
    scan_wb(seen)
    scan_yandex(seen)
    return seen


def source_health(today):
    checks = (("ozon", "raw_ozon_accrual", 3),
              ("wb", "raw_wb_report", 10),
              ("yandex", "raw_yandex_services", 3))
    warnings = []
    for platform, table, limit in checks:
        row = db.query("SELECT max(loaded_at)::date AS latest FROM " + table)[0]
        latest = row["latest"]
        if latest is None or (today - latest).days > limit:
            warnings.append(f"{PLATFORM[platform]}: последняя загрузка "
                            f"{latest:%d.%m.%Y}" if latest else
                            f"{PLATFORM[platform]}: данных нет")
    return warnings


def _fmt(item, key):
    platform, kind, code = key
    accounts = ", ".join(sorted(item["accounts"]))
    amount = f"{item['amount']:,.2f}".replace(",", " ").replace(".", ",")
    label = item["label"][:130]
    return (f"• {PLATFORM[platform]} · {accounts}: {label} "
            f"({kind}, код {code})\n"
            f"  с {item['first']:%d.%m.%Y}, {item['rows']} строк, {amount} ₽")


def _messages(new, warnings):
    blocks = []
    if new:
        blocks.append("Новые виды списаний и услуг МП:")
        blocks.extend(_fmt(item, key) for key, item in new)
    if warnings:
        blocks.append("⚠ Данные требуют проверки:")
        blocks.extend(warnings)
    if not blocks:
        return []
    messages, message = [], ""
    for block in blocks:
        if message and len(message) + len(block) + 2 > 3900:
            messages.append(message)
            message = ""
        message += ("\n" if message else "") + block
    if message:
        messages.append(message)
    return messages


def _send(message):
    if not TOKEN or not NATALIA_ID:
        raise RuntimeError("не настроен Telegram-бот или адресат Наталии")
    response = requests.post("https://api.telegram.org/bot" + TOKEN + "/sendMessage",
                             data={"chat_id": NATALIA_ID, "text": message,
                                   "disable_web_page_preview": "true"}, timeout=30)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram не подтвердил доставку")


def _save(keys, seen):
    if not keys:
        return
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for platform, kind, code in keys:
                item = seen[(platform, kind, code)]
                cur.execute("""INSERT INTO fin_charge_watch
                               (platform, kind, code, first_seen, last_seen)
                               VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (platform, kind, code) DO NOTHING""",
                            (platform, kind, code, item["first"], item["last"]))


def run(send=False):
    seen = scan()
    known = {(r["platform"], r["kind"], r["code"]) for r in db.query(
        "SELECT platform, kind, code FROM fin_charge_watch")}
    if not known:
        if send:
            _save(list(seen), seen)
        print(f"Первичная база: {len(seen)} видов; исторические списания не рассылаются")
        return 0
    new = sorted(((key, item) for key, item in seen.items() if key not in known),
                 key=lambda pair: (pair[1]["first"], pair[0]))
    warnings = source_health(dt.date.today())
    messages = _messages(new, warnings)
    if not send:
        print(f"Сухой прогон: новых видов {len(new)}, предупреждений {len(warnings)}")
        for message in messages:
            print(message[:3900])
        return len(new)
    for message in messages:
        _send(message)
    _save([key for key, _ in new], seen)
    print(f"Наталии: новых видов {len(new)}, предупреждений {len(warnings)}, сообщений {len(messages)}")
    return len(new)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="отправить новые виды Наталии")
    args = parser.parse_args()
    run(send=args.send)
