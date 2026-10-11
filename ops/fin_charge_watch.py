# поток: fin
"""Weekly alert about newly observed marketplace charges and service names.

Reads raw finance data and optionally refreshes WB via its current Finance API.
First run seeds the historical baseline; later runs send only unseen types to
Natalia. --send delivers Telegram, default is preview.
"""
import argparse
import datetime as dt
import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import requests
import psycopg2.extras
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
WB_FINANCE_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"


def collect_wb(today):
    """Fetch the last four weeks via the current WB Finance API, preserving raw rows.

    Only tokens verified for Finance access are used. Responses are streamed to disk
    before parsing, as a weekly detail report can exceed 1 MB.
    """
    failures = []
    for account, suffix in (("wb_acc1", "ACC1"), ("wb_acc2", "ACC2")):
        token = os.getenv("WB_TOKEN_FINANCE_" + suffix, "").strip()
        if not token and account == "wb_acc1":
            # Проверено 11.10: существующий токен ACC1 имеет Finance-scope,
            # новый POST /sales-reports/detailed возвращает 200.
            token = os.getenv("WB_TOKEN_PRICES_ACC1", "").strip()
        if not token:
            failures.append(f"ВБ {ACCOUNT[account]}: нет токена категории «Финансы»")
            continue
        cursor = 0
        received = 0
        try:
            while True:
                body = {"dateFrom": (today - dt.timedelta(days=28)).isoformat(),
                        "dateTo": today.isoformat(), "limit": 100000,
                        "rrdId": cursor, "period": "weekly"}
                with requests.post(WB_FINANCE_URL, headers={"Authorization": token},
                                   json=body, stream=True, timeout=(20, 180)) as response:
                    response.raise_for_status()
                    with tempfile.TemporaryFile(mode="w+b") as saved:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                saved.write(chunk)
                        saved.seek(0)
                        rows = json.load(saved)
                if not isinstance(rows, list):
                    raise RuntimeError("ответ Finance API не является списком")
                if rows:
                    values = []
                    for row in rows:
                        rrd_id = row.get("rrdId")
                        day = row.get("createDate")
                        if rrd_id is None or not day:
                            raise RuntimeError("в строке WB нет rrdId или createDate")
                        values.append((account, int(rrd_id), dt.date.fromisoformat(day[:10]),
                                       psycopg2.extras.Json(row)))
                    with db.get_conn() as conn:
                        with conn.cursor() as cur:
                            psycopg2.extras.execute_batch(cur, """
                                INSERT INTO raw_wb_finance_detail
                                    (account, rrd_id, report_date, payload)
                                VALUES (%s,%s,%s,%s)
                                ON CONFLICT (account, rrd_id) DO UPDATE SET
                                    report_date=EXCLUDED.report_date,
                                    payload=EXCLUDED.payload,
                                    loaded_at=now()""", values, page_size=500)
                received += len(rows)
                if len(rows) < 100000:
                    break
                next_cursor = int(rows[-1]["rrdId"])
                if next_cursor <= cursor:
                    raise RuntimeError("пагинация WB не продвинулась")
                cursor = next_cursor
                time.sleep(61)  # официальный лимит: один запрос в минуту на кабинет
            print(f"ВБ {ACCOUNT[account]}: получено {received} строк нового Finance API")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            failures.append(f"ВБ {ACCOUNT[account]}: новый Finance API — {type(exc).__name__}")
    return failures


def _money(value):
    return Decimal(str(value or 0))


def _add(seen, platform, kind, code, label, account, day, amount,
         period_from=None, period_to=None):
    key = (platform, kind, code)
    if key not in seen:
        seen[key] = {"label": label.strip() or code, "first": day, "last": day,
                     "accounts": set(), "amount": Decimal(0), "rows": 0,
                     "daily": {}, "daily_account": {}}
    item = seen[key]
    item["first"] = min(item["first"], day)
    item["last"] = max(item["last"], day)
    account_name = ACCOUNT.get(account, account)
    item["accounts"].add(account_name)
    item["amount"] += amount
    item["rows"] += 1
    daily = item["daily"].setdefault(day, {"amount": Decimal(0), "rows": 0,
                                          "from": None, "to": None})
    daily["amount"] += amount
    daily["rows"] += 1
    account_day = item["daily_account"].setdefault((day, account_name),
                                                     {"amount": Decimal(0), "rows": 0})
    account_day["amount"] += amount
    account_day["rows"] += 1
    if period_from:
        daily["from"] = min(daily["from"], period_from) if daily["from"] else period_from
    if period_to:
        daily["to"] = max(daily["to"], period_to) if daily["to"] else period_to


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

    rows = db.query("""SELECT account, report_date, payload
                      FROM raw_wb_finance_detail""")
    for r in rows:
        p = r["payload"]
        op = p.get("sellerOperName") or ""
        bonus = p.get("bonusTypeName") or ""
        for field, kind in (("deduction", "удержание"), ("penalty", "штраф"),
                            ("deliveryService", "логистика"), ("paidStorage", "хранение"),
                            ("paidAcceptance", "приёмка"), ("cashbackAmount", "баллы")):
            amount = _money(p.get(field))
            if amount <= 0:
                continue
            name = wb_norm((bonus if field in ("deduction", "penalty") else op) or op or kind)
            if field == "deliveryService" and _money(p.get("returnAmount")) > 0:
                name += " (обратная)"
            _add(seen, "wb", kind, name, name, r["account"], r["report_date"], amount,
                 p.get("dateFrom"), p.get("dateTo"))


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
              ("yandex", "raw_yandex_services", 3))
    warnings = []
    for platform, table, limit in checks:
        row = db.query("SELECT max(loaded_at)::date AS latest FROM " + table)[0]
        latest = row["latest"]
        if latest is None or (today - latest).days > limit:
            warnings.append(f"{PLATFORM[platform]}: последняя загрузка "
                            f"{latest:%d.%m.%Y}" if latest else
                            f"{PLATFORM[platform]}: данных нет")
    for account in ("wb_acc1", "wb_acc2"):
        old = db.query("""SELECT max((payload->>'create_dt')::date) AS latest
                          FROM raw_wb_report WHERE account=%s""", (account,))[0]["latest"]
        new = db.query("""SELECT max(report_date) AS latest
                          FROM raw_wb_finance_detail WHERE account=%s""", (account,))[0]["latest"]
        latest = max((x for x in (old, new) if x), default=None)
        if latest is None or (today - latest).days > 14:
            warnings.append(f"ВБ {ACCOUNT[account]}: последний отчёт "
                            f"{latest:%d.%m.%Y}" if latest else
                            f"ВБ {ACCOUNT[account]}: отчётов нет")
    return warnings


def _rub(amount):
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def _fmt(item, key, week_start, week_end):
    platform, kind, code = key
    label = item["label"][:130]
    days = [v for day, v in item["daily"].items() if week_start <= day <= week_end]
    selected = {day for day in item["daily"] if week_start <= day <= week_end}
    if days:
        amount = sum((v["amount"] for v in days), Decimal(0))
        count = sum(v["rows"] for v in days)
        period = f"за {week_start:%d.%m}–{week_end:%d.%m.%Y}"
        if platform == "wb":
            period += " (дата формирования отчёта)"
            starts = [v["from"] for v in days if v["from"]]
            ends = [v["to"] for v in days if v["to"]]
            if starts and ends:
                period += f"; услуги за {min(starts)[8:10]}.{min(starts)[5:7]}–{max(ends)[8:10]}.{max(ends)[5:7]}.{max(ends)[:4]}"
    else:
        count, amount = item["rows"], item["amount"]
        period = f"за догруженный период {item['first']:%d.%m.%Y}–{item['last']:%d.%m.%Y}"
        selected = set(item["daily"])
    by_account = {}
    for (day, account), value in item["daily_account"].items():
        if day not in selected:
            continue
        total = by_account.setdefault(account, {"rows": 0, "amount": Decimal(0)})
        total["rows"] += value["rows"]
        total["amount"] += value["amount"]
    accounts = ", ".join(sorted(by_account))
    breakdown = ""
    if len(by_account) > 1:
        breakdown = "\n  " + "; ".join(
            f"{account}: {value['rows']} / {_rub(value['amount'])} ₽"
            for account, value in sorted(by_account.items()))
    return (f"• {PLATFORM[platform]} · {accounts}: {label} "
            f"({kind}, код {code})\n"
            f"  {count} списаний, {_rub(amount)} ₽ {period}{breakdown}")


def _messages(new, warnings, week_start, week_end):
    blocks = []
    if new:
        blocks.append(f"Новые виды списаний и услуг МП · проверка за "
                      f"{week_start:%d.%m}–{week_end:%d.%m.%Y}:")
        blocks.extend(_fmt(item, key, week_start, week_end) for key, item in new)
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


def run(send=False, collect_wb_data=False, today=None):
    today = today or dt.date.today()
    week_end = today - dt.timedelta(days=today.weekday() + 1)
    week_start = week_end - dt.timedelta(days=6)
    collection_warnings = collect_wb(today) if collect_wb_data else []
    seen = scan()
    known = {(r["platform"], r["kind"], r["code"]) for r in db.query(
        "SELECT platform, kind, code FROM fin_charge_watch")}
    if not known:
        if send:
            _save(list(seen), seen)
        print(f"Первичная база: {len(seen)} видов; исторические списания не рассылаются")
        return 0
    new = sorted(((key, item) for key, item in seen.items()
                  if key not in known and item["first"] <= week_end),
                 key=lambda pair: (pair[1]["first"], pair[0]))
    warnings = collection_warnings + source_health(today)
    messages = _messages(new, warnings, week_start, week_end)
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
    parser.add_argument("--collect-wb", action="store_true", help="собрать отчёты из нового Finance API ВБ")
    args = parser.parse_args()
    run(send=args.send, collect_wb_data=args.collect_wb)
