# поток: ret
"""Возвраты Яндекс.Маркета: GET /campaigns/{id}/returns по всем кампаниям.

Штрихкода/QR Partner API не отдаёт — вместо него у точки выдачи есть `instruction`.
Только чтение.
"""
import os
import time

from dotenv import load_dotenv

from returns_bot import pending
from returns_bot.net import request_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"
PAGE = 100

_names = {}


def _headers():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    if not key:
        raise RuntimeError("нет YANDEX_API_KEY_ACC1")
    return {"Api-Key": key}


def campaigns():
    raw = os.getenv("YANDEX_CAMPAIGN_ID_ACC1", "") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def campaign_name(cid):
    """Человеческое имя магазина (кэш на процесс)."""
    if cid in _names:
        return _names[cid]
    try:
        j = request_json("GET", f"{API}/campaigns/{cid}", headers=_headers())
        _names[cid] = (j.get("campaign") or {}).get("domain") or cid
    except Exception:
        _names[cid] = cid
    return _names[cid]


def fetch_raw(cid):
    out, token = [], None
    while True:
        params = {"limit": PAGE}
        if token:
            params["page_token"] = token
        j = request_json("GET", f"{API}/campaigns/{cid}/returns",
                         headers=_headers(), params=params)
        res = j.get("result") or {}
        out += res.get("returns") or []
        token = (res.get("paging") or {}).get("nextPageToken")
        if not token:
            break
        time.sleep(0.3)
    return out


def _address(a):
    """Адрес точки выдачи Яндекс отдаёт объектом — собираем в строку."""
    if not a:
        return None
    if isinstance(a, str):
        return a
    city = (a.get("city") or "").split(",")[0]           # приходит «Москва,Москва»
    parts = [a.get("postcode"), city, a.get("street"), a.get("house")]
    return ", ".join(p for p in parts if p) or None


def normalize(cid, raw):
    pvz = raw.get("logisticPickupPoint") or {}
    status = raw.get("shipmentStatus") or ""
    amount = raw.get("amount") or {}

    head = {
        "platform": "yandex",
        "account": ACCOUNT,
        "return_id": str(raw.get("id")),
        "campaign": campaign_name(cid),
        "order_number": str(raw.get("orderId")) if raw.get("orderId") else None,
        "return_type": raw.get("returnType"),
        "scheme": None,
        "status_raw": status,
        "status_name": pending.YANDEX_STATUS_RU.get(status, status),
        "stage": pending.yandex_stage(status),
        "pvz_id": str(pvz.get("id")) if pvz.get("id") is not None else None,
        "pvz_name": pvz.get("name"),
        "pvz_address": _address(pvz.get("address")),
        "pvz_instruction": pvz.get("instruction"),
        "where_now": pvz.get("name"),
        "barcode": None,
        "created_at": raw.get("creationDate"),
        "arrived_at": None,
        "deadline_at": raw.get("pickupTillDate"),
        "storage_days": None,
        "storage_sum": None,
        "amount": amount.get("value") if amount else raw.get("refundAmount"),
    }
    items = []
    for i, it in enumerate(raw.get("items") or []):
        items.append({
            "platform": "yandex", "account": ACCOUNT, "return_id": head["return_id"], "seq": i,
            "sku": str(it.get("marketSku")) if it.get("marketSku") else None,
            "offer_id": it.get("shopSku"),
            "name": it.get("shopSku"),   # названия товара Partner API в возврате не отдаёт
            "qty": it.get("count"),
            "price": None,
        })
    return head, items


def collect(cids=None, quick=False):
    out = []
    for cid in (cids or campaigns()):
        for raw in fetch_raw(cid):
            head, items = normalize(cid, raw)
            out.append((head, items, raw))
    return out
