# поток: ret
"""Возвраты WB: GET seller-analytics-api /api/v1/analytics/goods-return.

Найдено 02.08.2026 через раздел «Аналитика» — в Marketplace API физических возвратов нет
(`/api/v3/supplies/returns` не существует, см. docs/reports/returns_api_recon.md). Этот отчёт
даёт ровно то, что нужно боту: статус, адрес пункта, номер стикера на коробке.

Особенности:
  * окно не больше **31 дня**, режется по `orderDt` (дате заказа) → идём несколькими окнами
    назад, чтобы поймать возвраты по старым заказам, которые ещё висят;
  * `expiredDt` у живых строк пустой — срока «забрать до» WB не даёт, считаем по `first_seen`;
  * одна строка = одна коробка (`shkId`), поэтому позиция всегда одна;
  * в сводке показываем `orderId` — это номер сборочного задания WB, он же имя отгрузки
    в МойСкладе; `shkId`/`stickerId` в МС не ищутся (это ШК наклейки), идут отдельной строкой;
  * названия товара в отчёте нет — оно подтягивается в render.py из `wb_cards` по `nm_id`.

Ключ — базовый токен аккаунта (скоуп «Аналитика»), НЕ возвратный: `returns-api` отдаёт
только заявки покупателей, без адреса и стикера. Только чтение.
"""
import os
import time
from datetime import date, timedelta

from dotenv import load_dotenv

from returns_bot import pending
from returns_bot.net import request_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API = "https://seller-analytics-api.wildberries.ru/api/v1/analytics/goods-return"
CRED_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
ACCOUNT_TITLE = {"wb_acc1": "Цифровой", "wb_acc2": "Дисквэр"}

WINDOW_DAYS = 31          # жёсткий потолок API
LOOKBACK_DAYS = 93        # три окна назад: живые возвраты по заказам до трёх месяцев давности
PAUSE = 8                 # лимитер аналитики WB злой; ретраи на 429 — в net.request_json


def _token(account):
    key = os.getenv(CRED_ENV[account])
    if not key:
        raise RuntimeError(f"нет {CRED_ENV[account]}")
    return key


def windows(today=None):
    """Окна по 31 дню назад, от свежего к старому."""
    today = today or date.today()
    out, end = [], today
    while (today - end).days < LOOKBACK_DAYS:
        start = end - timedelta(days=WINDOW_DAYS)
        out.append((start, end))
        end = start
    return out


def fetch_raw(account, today=None):
    """Строки отчёта без дублей: окна перекрываются краями."""
    headers = {"Authorization": _token(account)}
    seen, out = set(), []
    for i, (d1, d2) in enumerate(windows(today)):
        if i:
            time.sleep(PAUSE)
        j = request_json("GET", API, headers=headers,
                         params={"dateFrom": d1.isoformat(), "dateTo": d2.isoformat()})
        for row in (j.get("report") or []):
            key = row.get("shkId")
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
    return out


def normalize(account, raw):
    status = (raw.get("status") or "").strip()
    active = bool(raw.get("isStatusActive"))
    head = {
        "platform": "wb",
        "account": account,
        # ключ — коробка: srid тоже уникален, но ехать в пункт нужно за физическим местом
        "return_id": str(raw.get("shkId")),
        "campaign": None,
        # Номер сборочного задания WB = имя отгрузки в МойСкладе (проверено 02.08.2026:
        # orderId 5275090801 = demand 5275090801 от 06.07). Стикер (shkId/stickerId) в МС не
        # ищется вообще — это ШК наклейки на коробке, для получения он не нужен.
        # orderId=0 у возвратов СО СКЛАДА ВБ (брак / неверное вложение / по инициативе продавца):
        # заказа у нас нет → номер оставляем пустым, render помечает такие «ФБО».
        "order_number": str(raw["orderId"]) if str(raw.get("orderId") or "0") != "0" else None,
        "return_type": raw.get("returnType"),
        "scheme": None,
        "status_raw": status,
        "status_name": status,
        "stage": pending.wb_stage(status, active),
        "pvz_id": str(raw.get("dstOfficeId")) if raw.get("dstOfficeId") else None,
        "pvz_name": None,                       # отдельного имени точки в отчёте нет, только адрес
        "pvz_address": raw.get("dstOfficeAddress"),
        "pvz_instruction": None,
        "where_now": raw.get("dstOfficeAddress"),
        # стикер на коробке — его и называют в пункте; картинку не рисуем (см. запрет в брифе)
        "barcode": str(raw.get("stickerId")) if raw.get("stickerId") else None,
        "created_at": raw.get("orderDt"),
        "arrived_at": raw.get("readyToReturnDt"),
        "deadline_at": raw.get("expiredDt"),    # у живых строк пусто — WB срока не отдаёт
        "storage_days": None,
        "storage_sum": None,
        "amount": None,                         # денег в этом отчёте нет, они в финотчёте (поток fin)
    }
    items = [{
        "platform": "wb", "account": account, "return_id": head["return_id"], "seq": 0,
        "sku": str(raw.get("nmId")) if raw.get("nmId") else None,
        "offer_id": str(raw.get("nmId")) if raw.get("nmId") else None,
        "name": raw.get("subjectName"),         # имя карточки добираем в render.py из wb_cards
        "qty": 1,                               # строка отчёта = одна коробка
        "price": None,
    }]
    return head, items


def collect(accounts=None):
    out = []
    for account in (accounts or CRED_ENV):
        for raw in fetch_raw(account):
            head, items = normalize(account, raw)
            out.append((head, items, raw))
    return out
