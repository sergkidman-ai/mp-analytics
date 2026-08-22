# поток: ev
"""yandex_stocks.py — остатки на складах Яндекс.Маркета (FBY).

Зачем: с 01.09.2026 Маркет вводит фиксированный льготный срок хранения (наши категории — 120
дней с даты поступления поставки, КГТ — 30), после него хранение платное. Залежавшийся товар
нужно вывозить так же, как с Ozon FBO, — для этого нужен ежедневный снимок остатков.

Источник: POST /campaigns/{campaignId}/offers/stocks (withTurnover=true).
FBY-магазин кабинета — «Цифровой квадрат», campaignId 21589415; в списке FBS-кампаний его нет.

ВАЖНО: у этой кампании в ЛК выключен доступ к API (ответ 403 API_DISABLED). Пока владелец
кабинета его не включит, сбор возвращает 0 строк и печатает причину — это не поломка коллектора.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"
FBY_CAMPAIGN = int(os.getenv("YANDEX_CAMPAIGN_FBY_ACC1", "21589415"))
STOCK_TYPES = {"AVAILABLE": "available", "FREEZE": "frozen", "DEFECT": "defect", "EXPIRED": "expired"}


class ApiDisabled(RuntimeError):
    """Доступ к API магазина выключен в личном кабинете Маркета."""


def _headers():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    if not key:
        raise RuntimeError("YANDEX_API_KEY_ACC1 не задан в окружении")
    return {"Api-Key": key, "Content-Type": "application/json"}


def fetch(campaign_id=FBY_CAMPAIGN):
    """Все остатки FBY одной кампании: [{warehouse_id, warehouse, offer_id, ...}]."""
    head, out, token = _headers(), [], None
    while True:
        params = {"limit": 200}
        if token:
            params["page_token"] = token
        r = requests.post(f"{API}/campaigns/{campaign_id}/offers/stocks",
                          headers=head, params=params, json={"withTurnover": True}, timeout=60)
        if r.status_code == 403 and "API_DISABLED" in r.text:
            raise ApiDisabled(f"кампания {campaign_id}: доступ к API выключен в ЛК Маркета")
        r.raise_for_status()
        res = r.json().get("result", {})
        for wh in res.get("warehouses", []):
            for off in wh.get("offers", []):
                st = {s.get("type"): s.get("count", 0) for s in off.get("stocks", [])}
                turn = off.get("turnoverSummary") or {}
                row = {"account": ACCOUNT, "campaign_id": campaign_id,
                       "warehouse_id": wh.get("warehouseId"), "warehouse": wh.get("name"),
                       "offer_id": off.get("offerId"), "updated_at": off.get("updatedAt"),
                       "turnover_days": turn.get("turnoverDays"), "turnover": turn.get("turnover")}
                row.update({col: int(st.get(t, 0) or 0) for t, col in STOCK_TYPES.items()})
                out.append(row)
        token = (res.get("paging") or {}).get("nextPageToken")
        if not token:
            break
    return out


def main():
    try:
        rows = fetch()
    except ApiDisabled as e:
        print(f"остатки FBY не собраны — {e}.\n"
              f"Включить: ЛК Маркета → Настройки → Доступ к API → магазин «Цифровой квадрат» (FBY).",
              flush=True)
        return 0
    if not rows:
        print("остатков FBY нет (склад пуст)", flush=True)
        return 0
    day = db.query("SELECT current_date d")[0]["d"]
    for r in rows:
        r["captured_at"] = day
    db.upsert("ya_fby_stock", rows,
              ["account", "campaign_id", "warehouse_id", "offer_id", "captured_at"])
    qty = sum(r["available"] + r["frozen"] for r in rows)
    print(f"FBY {day}: {len(rows)} позиций, {qty} шт, складов {len({r['warehouse_id'] for r in rows})}",
          flush=True)
    return len(rows)


if __name__ == "__main__":
    main()
