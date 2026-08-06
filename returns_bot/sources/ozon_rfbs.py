# поток: ret
"""Возвраты Real-FBS (rFBS, экспресс-заказы): POST /v2/returns/rfbs/list + /v2/returns/rfbs/get.

Зачем отдельный источник: `/v1/returns/list` (модуль `ozon.py`) rFBS-возвраты НЕ отдаёт —
там только Fbs и Fbo. Проверено 06.08.2026: живой возврат 100264955 (Дисквэр,
«В пункте выдачи», трек 802169…) в выдаче `/v1/returns/list` отсутствует.

Такой возврат покупатель сдаёт Почтой России, и получаем мы его на почте **по трек-номеру** —
он приходит только в деталях (`/v2/returns/rfbs/get` → `ru_post_tracking_number`), в списке его нет.
Поэтому: список фильтруем по `group_state = delivering` (всё, что физически едет или лежит),
детали дёргаем только по строкам, которые нам интересны (pickup/transit) — 2–5 запросов на прогон.

Особенности API (разведка 06.08.2026):
- `offset` в списке **игнорируется** (limit=100 и limit=1000 дают одно и то же), `last_id` тоже:
  пагинации нет. Спасает фильтр: `group_state=["delivering"]` даёт 64 строки у acc1 и 10 у acc2,
  в лимит 1000 помещается с запасом. Фильтр `state` (строкой) молча игнорируется — не использовать.
- `/v2/returns/rfbs/get` возвращает объект в ключе `returns` (единственном числе там нет).
- Адреса точки API не даёт вообще: есть только `warehouse_id` и способ возврата
  («Почтой России»). Куда ехать — определяет трек, поэтому он в сводке обязателен.

Только чтение: `available_actions` (одобрить/отклонить/вернуть деньги) не трогаем.
"""
import time

from returns_bot import pending
from returns_bot.net import request_json
from returns_bot.sources.ozon import ACCOUNT_TITLE, API, CRED_ENV, _headers, _money  # noqa: F401

# Что запрашиваем: группа «доставляется» — сюда попадают Ожидает отправки / Едет к вам /
# В пункте выдачи / Получен. Остальные группы (new, approved, rejected, arbitration,
# utilization) — деньги и споры, физики возврата там нет.
GROUP_STATES = ["delivering"]
PAGE = 1000

# По каким строкам идём за деталями (ради трека). Получен/ArrivedForResale уже у нас — незачем.
DETAIL_STATES = pending.OZON_RFBS_PICKUP | pending.OZON_RFBS_TRANSIT


def fetch_raw(account):
    """Строки списка rFBS-возвратов в стадии доставки. Один запрос на аккаунт."""
    j = request_json("POST", f"{API}/v2/returns/rfbs/list", headers=_headers(account),
                     json_body={"filter": {"group_state": GROUP_STATES},
                                "limit": PAGE, "offset": 0})
    return j.get("returns") or []


def fetch_detail(account, return_id):
    """Детали возврата: трек Почты, способ возврата, комментарий покупателя."""
    j = request_json("POST", f"{API}/v2/returns/rfbs/get", headers=_headers(account),
                     json_body={"return_id": int(return_id)})
    return (j or {}).get("returns") or {}


def normalize(account, row, detail=None):
    """Строка списка (+ детали, если брали) → (шапка, позиции)."""
    detail = detail or {}
    state = row.get("state") or {}
    product = row.get("product") or detail.get("product") or {}
    method = detail.get("client_return_method_type") or {}
    sys_name = state.get("state") or ""

    head = {
        "platform": "ozon",
        "source": "ozon_rfbs",
        "account": account,
        # префикс — чтобы id не столкнулся с id из /v1/returns/list (там своё пространство)
        "return_id": f"rfbs-{row.get('return_id')}",
        "campaign": None,
        "order_number": row.get("posting_number") or row.get("order_number") or None,
        "return_type": (detail.get("return_reason") or {}).get("name"),
        "scheme": "rFbs",
        "status_raw": sys_name,
        "status_name": state.get("state_name"),
        "stage": pending.ozon_rfbs_stage(sys_name),
        # адреса точки в API нет — печатаем способ возврата («Почтой России»), ехать по треку
        "pvz_id": str(detail["warehouse_id"]) if detail.get("warehouse_id") else None,
        "pvz_name": method.get("name") or "Почтой России",
        "pvz_address": None,
        "pvz_instruction": None,
        "where_now": None,
        "barcode": None,
        "track_number": detail.get("ru_post_tracking_number") or None,
        "created_at": row.get("created_at") or detail.get("created_at"),
        "arrived_at": None,
        "deadline_at": None,
        "storage_days": None,
        "storage_sum": None,
        "amount": product.get("price"),
    }
    items = []
    if product.get("sku") or product.get("offer_id"):
        items.append({
            "platform": "ozon", "account": account, "return_id": head["return_id"], "seq": 0,
            "sku": str(product.get("sku")) if product.get("sku") else None,
            "offer_id": product.get("offer_id"),
            "name": product.get("name"),
            "qty": product.get("quantity") or 1,
            "price": product.get("price"),
        })
    return head, items


def collect(accounts=None):
    """[(head, items, raw), ...] по всем аккаунтам Ozon."""
    out = []
    for account in (accounts or list(CRED_ENV)):
        for row in fetch_raw(account):
            detail = None
            if (row.get("state") or {}).get("state") in DETAIL_STATES:
                detail = fetch_detail(account, row["return_id"])
                time.sleep(0.3)                      # /v2/returns/rfbs/* быстро отдаёт 429
            head, items = normalize(account, row, detail)
            out.append((head, items, {"list": row, "detail": detail}))
    return out
