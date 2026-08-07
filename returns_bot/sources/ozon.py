# поток: ret
"""Возвраты Ozon: POST /v1/returns/list + штрихкод получения /v1/return/giveout/*.

Отдаёт нормализованные записи под mp_returns/mp_return_items. Только чтение.
"""
import base64
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from returns_bot import pending
from returns_bot.net import request_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API = "https://api-seller.ozon.ru"
CRED_ENV = {
    "oz_acc1": ("OZON_CLIENT_ID_ACC1", "OZON_API_KEY_ACC1"),
    "oz_acc2": ("OZON_CLIENT_ID_ACC2", "OZON_API_KEY_ACC2"),
}
ACCOUNT_TITLE = {"oz_acc1": "Цифровой", "oz_acc2": "Дисквэр"}
PAGE = 500


def _headers(account):
    ci, ak = CRED_ENV[account]
    cid, key = os.getenv(ci), os.getenv(ak)
    if not cid or not key:
        raise RuntimeError(f"нет кредов Ozon для {account} ({ci}/{ak})")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


def fetch_raw(account):
    """Все возвраты аккаунта (пагинация по last_id). ~7 запросов на аккаунт."""
    out, last_id = [], 0
    while True:
        j = request_json("POST", f"{API}/v1/returns/list", headers=_headers(account),
                         json_body={"filter": {}, "limit": PAGE, "last_id": last_id})
        chunk = j.get("returns") or []
        out += chunk
        if not chunk or not j.get("has_next"):
            break
        last_id = chunk[-1]["id"]
        time.sleep(0.3)
    return out


def giveout_barcode(account):
    """Штрихкод получения возвратов: (значение, PNG в base64). Один на аккаунт."""
    h = _headers(account)
    value = (request_json("POST", f"{API}/v1/return/giveout/barcode", headers=h,
                          json_body={}) or {}).get("barcode")
    png = (request_json("POST", f"{API}/v1/return/giveout/get-png", headers=h,
                        json_body={}) or {}).get("png")
    return value, png


def giveout_pdf(account):
    """Тот же штрихкод в печатном виде (base64 PDF): с бумаги сканер читает увереннее.

    `/barcode-reset` рядом НЕ трогаем — он меняет код в кабинете, а у нас только чтение.
    """
    return (request_json("POST", f"{API}/v1/return/giveout/get-pdf",
                         headers=_headers(account), json_body={}) or {}).get("pdf")


# Срок жизни штрихкода Ozon в API не отдаёт нигде: /barcode даёт только значение,
# /giveout/info и /giveout/list по нашим аккаунтам пусты. Дата напечатана на самом бланке
# строкой «Штрихкод действует до 08.08.2026 11:56:15» — оттуда и берём.
TILL_RE = re.compile(r"действует\s+до\s+(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})")
MSK = timezone(timedelta(hours=3))     # на бланке московское время, сервер живёт в UTC


def _pdf_text(pdf_bytes):
    """Текст бланка. Через poppler: шрифты в PDF подмножеством, свой разбор врёт."""
    try:
        p = subprocess.run(["pdftotext", "-", "-"], input=pdf_bytes,
                           capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""                      # нет pdftotext — просто не покажем срок, не падаем
    return p.stdout.decode("utf-8", "replace")


def giveout_valid_till(pdf_bytes):
    """До какого момента код примут в пункте (МСК). None, если бланк не разобрался.

    Код живёт сутки с выпуска и от повторных запросов НЕ продлевается: два запроса
    с разницей в минуту вернули один и тот же срок (проверено 07.08.2026).
    """
    m = TILL_RE.search(_pdf_text(pdf_bytes or b""))
    if not m:
        return None
    d, mo, y, hh, mm = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=MSK)
    except ValueError:
        return None


def giveout(account):
    """Всё про штрихкод получения одним заходом: значение, PNG, печатный бланк и срок."""
    value, png_b64 = giveout_barcode(account)
    try:
        pdf = base64.b64decode(giveout_pdf(account) or "") or None
    except Exception:
        pdf = None
    return {"value": value, "png_b64": png_b64, "pdf": pdf,
            "till": giveout_valid_till(pdf) if pdf else None}


def _money(v):
    """Деньги Ozon приходят как {'currency_code': 'RUB', 'price': 3304}."""
    if isinstance(v, dict):
        return v.get("price")
    return v


def _ts(v):
    return None if v in (None, "") else v


def normalize(account, raw):
    """Сырой возврат Ozon → (шапка, позиции)."""
    visual = raw.get("visual") or {}
    status = visual.get("status") or {}
    sys_name = status.get("sys_name") or ""
    logistic = raw.get("logistic") or {}
    storage = raw.get("storage") or {}
    product = raw.get("product") or {}
    # target_place — куда приедет, там и забираем; place — где возврат физически сейчас.
    # У статуса «В пункте выдачи» они совпадают: товар уже на месте выдачи.
    place = raw.get("target_place") or raw.get("place") or {}
    now_place = raw.get("place") or {}

    head = {
        "platform": "ozon",
        "account": account,
        "return_id": str(raw.get("id")),
        "campaign": None,
        "order_number": raw.get("order_number") or raw.get("posting_number"),
        "return_type": raw.get("type"),
        "scheme": raw.get("schema"),
        "status_raw": sys_name,
        "status_name": status.get("display_name"),
        "stage": pending.ozon_stage(sys_name, logistic.get("final_moment")),
        "pvz_id": str(place.get("id")) if place.get("id") is not None else None,
        "pvz_name": place.get("name"),
        "pvz_address": place.get("address"),
        "pvz_instruction": None,
        "where_now": now_place.get("name"),
        "barcode": logistic.get("barcode"),
        "created_at": _ts(logistic.get("return_date")),
        "arrived_at": _ts(storage.get("arrived_moment")),
        "deadline_at": _ts(storage.get("utilization_forecast_date")),
        "storage_days": storage.get("days"),
        "storage_sum": _money(storage.get("sum")),
        "amount": _money(product.get("price")),
    }
    items = []
    if product.get("sku") or product.get("offer_id"):
        items.append({
            "platform": "ozon", "account": account, "return_id": head["return_id"], "seq": 0,
            "sku": str(product.get("sku")) if product.get("sku") else None,
            "offer_id": product.get("offer_id"),
            "name": product.get("name"),
            "qty": product.get("quantity"),
            "price": _money(product.get("price")),
        })
    return head, items


def collect(accounts=None, quick=False):
    """[(head, items, raw), ...] по всем аккаунтам Ozon."""
    out = []
    for account in (accounts or list(CRED_ENV)):
        for raw in fetch_raw(account):
            head, items = normalize(account, raw)
            out.append((head, items, raw))
    return out
