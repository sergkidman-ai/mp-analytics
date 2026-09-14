"""Отчёт Ozon по заказам → raw_ozon_orders_report.

Зачем отдельный сборщик: колонка «Выкуп товара» есть ТОЛЬКО в этом отчёте. Ни
/v3/posting/fbs/list, ни /v2/posting/fbo/list, ни /v3/posting/fbs/get её не отдают —
проверено. А без неё нельзя отделить отправления, по которым в ФТС отчитывается сам
Ozon (он выкупил товар и стал собственником), от наших.

Две особенности API, на которые легко напороться:
  * /v1/report/postings/create принимает РОВНО ОДНУ delivery_schema за запрос
    («DeliverySchema: value must contain no more than 1 item»), поэтому два вызова;
  * фильтр по датам — только processed_at (дата принятия в обработку). Даты доставки
    в фильтре нет, поэтому окно по processed_at берём с запасом назад, а отбираем
    потом по колонке «Дата доставки» — заказ, доставленный в августе, мог быть
    отгружен в июне;
  * окно processed_at не может быть длиннее трёх месяцев («Выберите период не более
    трёх месяцев»), поэтому запас назад режем на куски по WINDOW_DAYS.

Отчёт формируется асинхронно: create отдаёт код, info — статус и ссылку на CSV.

Запуск:  venv/bin/python -m collectors.ozon_orders_report --period 2026-08
"""
import argparse
import csv
import io
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import psycopg2.extras
import requests

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import _headers  # noqa: E402

CREATE_URL = "https://api-seller.ozon.ru/v1/report/postings/create"
INFO_URL = "https://api-seller.ozon.ru/v1/report/info"
ACCOUNTS = ("oz_acc1", "oz_acc2")
SCHEMAS = ("fbs", "fbo")

# Сколько дней до начала отчётного месяца захватывать по processed_at. Доставка в ЕАЭС
# идёт долго: заказ, доставленный 01.08, мог уйти в обработку в середине июня.
LOOKBACK_DAYS = 120

# Ozon отказывает, если окно processed_at длиннее трёх месяцев, — режем на куски.
WINDOW_DAYS = 85

POLL_EVERY = 5      # сек между опросами статуса
POLL_LIMIT = 80     # ~7 минут ожидания; дольше Ozon отчёт такого объёма не готовит

# Заголовки CSV → наши имена. Ozon иногда меняет регистр и пробелы, поэтому сверяем
# по очищенному ключу.
COLS = {
    "номер отправления": "posting_number",
    "номер заказа": "order_number",
    "статус": "status",
    "дата доставки": "delivered_at",
    "принят в обработку": "processed_at",
    "дата отгрузки": "shipped_at",
    "выкуп товара": "buyout",
    "сумма отправления": "posting_amount",
    "код валюты отправления": "posting_currency",
    "оплачено покупателем": "paid_by_customer",
    "код валюты покупателя": "customer_currency",
    "название товара": "product_name",
    "артикул": "offer_id",
    "sku": "sku",
    "количество": "qty",
}
BUYOUT_MARK = "подлежит выкупу"
DELIVERED = "доставлен"


def _norm(h):
    return (h or "").replace("﻿", "").strip().strip('"').lower()


def _parse_date(s):
    """Ozon пишет дату как «01.08.2026» или «01.08.2026 14:23». Пусто → None."""
    s = (s or "").strip()
    if not s:
        return None
    part = s.split()[0].replace("/", ".")
    try:
        d, m, y = part.split(".")
        return date(int(y), int(m), int(d))
    except ValueError:
        try:
            return date.fromisoformat(part[:10])
        except ValueError:
            return None


def _request_report(headers, schema, dt_from, dt_to):
    """Заказать отчёт и дождаться CSV. Возвращает bytes или None."""
    body = {"filter": {"processed_at_from": f"{dt_from}T00:00:00.000Z",
                       "processed_at_to": f"{dt_to}T23:59:59.999Z",
                       "delivery_schema": [schema]},
            "language": "DEFAULT"}
    r = requests.post(CREATE_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    code = r.json()["result"]["code"]

    info = {}
    for _ in range(POLL_LIMIT):
        time.sleep(POLL_EVERY)
        info = requests.post(INFO_URL, headers=headers, json={"code": code},
                             timeout=60).json().get("result", {})
        if info.get("status") in ("success", "failed"):
            break
    if info.get("status") != "success" or not info.get("file"):
        print(f"    отчёт {schema}: статус {info.get('status')} {info.get('error') or ''}",
              flush=True)
        return None
    return requests.get(info["file"], timeout=300).content


def _rows_from_csv(raw):
    """CSV Ozon → список словарей по нашим именам колонок."""
    text = raw.decode("utf-8-sig", errors="replace")
    rd = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(rd)
    if not rows:
        return []
    header = [_norm(h) for h in rows[0]]
    idx = {COLS[h]: i for i, h in enumerate(header) if h in COLS}
    if "posting_number" not in idx:
        raise RuntimeError(f"в отчёте нет колонки «Номер отправления»: {header[:8]}")
    out = []
    for r in rows[1:]:
        if not any(r):
            continue
        out.append({k: (r[i].strip() if i < len(r) else "") for k, i in idx.items()})
    return out


def collect(account, period, schemas=SCHEMAS):
    """Собрать отчёт за отчётный месяц period (date первого числа) в базу."""
    headers = _headers(account)
    # Верхняя граница окна — конец отчётного месяца: в обработку заказ принимается
    # всегда раньше доставки, значит доставленное в августе обработано не позже 31.08.
    nxt = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    dt_to = nxt - timedelta(days=1)
    dt_from = period - timedelta(days=LOOKBACK_DAYS)

    saved = 0
    for schema in schemas:
        rows = []
        w_from = dt_from
        while w_from <= dt_to:
            w_to = min(w_from + timedelta(days=WINDOW_DAYS - 1), dt_to)
            raw = _request_report(headers, schema, w_from, w_to)
            if raw:
                rows.extend(_rows_from_csv(raw))
            w_from = w_to + timedelta(days=1)
        if not rows:
            print(f"    {schema}: пусто", flush=True)
            continue

        # В отчёте строка на товар — схлопываем в отправление, товары кладём в payload.
        # Куски окон могут пересечься по границе — отправление просто перезапишется.
        by_posting = {}
        for r in rows:
            pn = r.get("posting_number") or ""
            if not pn:
                continue
            head = by_posting.setdefault(pn, {**r, "items": []})
            head["items"].append({k: r.get(k) for k in
                                  ("product_name", "offer_id", "sku", "qty",
                                   "paid_by_customer", "customer_currency")})

        batch = []
        for pn, p in by_posting.items():
            batch.append({
                "account": account,
                "posting_number": pn,
                "delivery_schema": schema,
                "status": p.get("status"),
                "delivered_at": _parse_date(p.get("delivered_at")),
                "is_buyout": BUYOUT_MARK in (p.get("buyout") or "").lower(),
                "payload": psycopg2.extras.Json(p),
            })
        if batch:
            db.upsert("raw_ozon_orders_report", batch, ["account", "posting_number"])
            saved += len(batch)
        print(f"    {schema}: строк {len(rows)}, отправлений {len(by_posting)}", flush=True)
    return saved


def main_period(period, accounts=ACCOUNTS):
    """Собрать отчёт по всем аккаунтам за отчётный месяц — точка входа для run_daily."""
    for acc in accounts:
        print(f"{acc}: отчёт по заказам за {period:%Y-%m}", flush=True)
        print(f"  сохранено отправлений: {collect(acc, period)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", help="отчётный месяц YYYY-MM (по умолчанию прошлый)")
    ap.add_argument("--account", choices=ACCOUNTS, help="только один аккаунт")
    args = ap.parse_args()

    if args.period:
        y, m = args.period.split("-")
        period = date(int(y), int(m), 1)
    else:
        period = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)

    main_period(period, [args.account] if args.account else ACCOUNTS)


if __name__ == "__main__":
    main()
