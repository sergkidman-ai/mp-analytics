# поток: ret
"""Вывоз со склада FBO: POST /v1/removal/from-stock/list и /v1/removal/from-supply/list.

Это не возврат покупателя, а наш товар, который Ozon везёт со своего склада в пункт выдачи:
брак и неликвид со стока (`from-stock`) и то, что не приняли из поставки (`from-supply`).
Физика та же, что у обычного возврата, — коробка ложится в ПВЗ и её надо забрать, поэтому
источник живёт в том же боте. В `/v1/returns/list` этих строк НЕТ (проверено 06.08.2026).

Единица показа — **коробка** (`box_id`), а не заявка: в одной заявке коробок несколько, едут
они из разных РФЦ и приезжают порознь. Ключ строки — `rm-<заявка>-<коробка>`.

Особенности API (разведка 06.08.2026):
- окно `date_from`/`date_to` — обязательное, не больше 3 месяцев и не меньше суток,
  формат YYYY-MM-DD (иначе 400 с текстом про SupplyReturnsSummaryReport);
- пагинация — строковый `last_id` из ответа (`offset` есть в теле, но не работает);
  `last_id: 0` числом = 400, начинать надо с пустого тела без ключа;
- статус коробки `box_state` («В пункте выдачи» / «В пути» / «Получена» / «Утилизирована» /
  «Компенсировано продавцу»), статус заявки `return_state` («Создаётся» / «Собирается на складе» /
  «В пути» / «Можно забирать всё» / «Завершено»). Пока коробка не собрана, `box_id = 0`
  и `box_state` пустой;
- `given_out_date` и `utilization_date` приходят пустыми даже у забранных коробок — сроком
  «забрать до» пользоваться нельзя, его в этой выдаче нет;
- адрес пункта — `destination_warehouse_address`, там же код склада (`МОСКВА_4048`).

Только чтение: заявку на вывоз API создавать не умеет (см. память ozon-fbo-removal-recon),
её по-прежнему оформляет человек в ЛК.
"""
from collections import OrderedDict
from datetime import date, timedelta

from returns_bot import pending
from returns_bot.net import request_json
from returns_bot.sources.ozon import ACCOUNT_TITLE, API, CRED_ENV, _headers  # noqa: F401

PAGE = 100
WINDOW_DAYS = 90          # максимум, который принимает API, — 3 месяца
WINDOWS = 2               # полгода назад: коробка живёт в пути и в пункте неделями
ENDPOINTS = [
    # (путь, scheme, префикс ключа)
    ("/v1/removal/from-stock/list", "FboRemoval", "rm"),
    ("/v1/removal/from-supply/list", "FboRemovalSupply", "rms"),
]


def _windows(today=None):
    """Окна по 3 месяца назад, как их принимает API."""
    end = today or date.today()
    out = []
    for _ in range(WINDOWS):
        start = end - timedelta(days=WINDOW_DAYS)
        out.append((start.isoformat(), end.isoformat()))
        end = start - timedelta(days=1)
    return out


def fetch_raw(account, path):
    """Строки отчёта о вывозе за полгода. Дедуп по (заявка, коробка, артикул, индекс)."""
    rows, seen = [], set()
    for date_from, date_to in _windows():
        last_id = ""
        while True:
            body = {"date_from": date_from, "date_to": date_to, "limit": PAGE}
            if last_id:
                body["last_id"] = last_id
            j = request_json("POST", f"{API}{path}", headers=_headers(account), json_body=body)
            chunk = j.get("returns_summary_report_rows") or []
            fresh = 0
            for r in chunk:
                key = (r.get("return_id"), r.get("box_id"), r.get("offer_id"), r.get("sku"),
                       r.get("return_created_at"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
                fresh += 1
            # страница без единой новой строки = выдача пошла по кругу, дальше не идём
            if not chunk or not j.get("last_id") or not fresh:
                break
            last_id = j["last_id"]
    return rows


def _box_key(row):
    """Коробка, за которой едут. Пока её не собрали (box_id = 0) — группируем по заявке."""
    box = row.get("box_id") or 0
    return str(row.get("return_id")), str(box)


def normalize(account, scheme, prefix, box_rows):
    """Все строки одной коробки → (шапка, позиции)."""
    first = box_rows[0]
    request_id, box_id = _box_key(first)
    box_state = (first.get("box_state") or "").strip()
    return_state = (first.get("return_state") or "").strip()
    delivered = box_state in pending.OZON_REMOVAL_PICKUP or box_state == "Получена"

    head = {
        "platform": "ozon",
        "source": "ozon_removal",
        "account": account,
        "return_id": f"{prefix}-{request_id}-{box_id}",
        "campaign": None,
        # номер заявки на вывоз — по нему позиция ищется в ЛК Ozon (FBO → Вывоз и утилизация)
        "order_number": request_id,
        "return_type": first.get("stock_type"),          # «Брак…» / «Доступно к продаже»
        "scheme": scheme,
        "status_raw": box_state or return_state,
        "status_name": box_state or return_state or None,
        "stage": pending.ozon_removal_stage(box_state, return_state),
        "pvz_id": None,
        "pvz_name": first.get("destination_warehouse_name"),
        "pvz_address": first.get("destination_warehouse_address"),
        "pvz_instruction": None,
        "where_now": first.get("clearing_warehouse_name"),   # РФЦ, откуда едет коробка
        "barcode": first.get("box_barcode") or None,
        "track_number": None,
        "created_at": first.get("return_created_at") or None,
        "arrived_at": (first.get("delivery_date") or None) if delivered else None,
        "deadline_at": None,                              # срока «забрать до» API не даёт
        "storage_days": None,
        "storage_sum": None,
        "amount": None,                                   # цены товара в отчёте о вывозе нет
    }

    # одинаковые артикулы в коробке приходят отдельными строками (itemIndex) — складываем
    merged = OrderedDict()
    for r in box_rows:
        key = (r.get("offer_id"), str(r.get("sku") or ""))
        cur = merged.setdefault(key, {"name": r.get("name"), "qty": 0})
        cur["qty"] += int(r.get("quantity_for_return") or 0) or 1
    items = []
    for seq, ((offer_id, sku), v) in enumerate(merged.items()):
        items.append({
            "platform": "ozon", "account": account, "return_id": head["return_id"], "seq": seq,
            "sku": sku or None, "offer_id": offer_id, "name": v["name"],
            "qty": v["qty"], "price": None,
        })
    return head, items


def collect(accounts=None):
    """[(head, items, raw), ...] по всем аккаунтам Ozon."""
    out = []
    for account in (accounts or list(CRED_ENV)):
        for path, scheme, prefix in ENDPOINTS:
            boxes = OrderedDict()
            for row in fetch_raw(account, path):
                boxes.setdefault(_box_key(row), []).append(row)
            for box_rows in boxes.values():
                head, items = normalize(account, scheme, prefix, box_rows)
                out.append((head, items, {"rows": box_rows}))
    return out
