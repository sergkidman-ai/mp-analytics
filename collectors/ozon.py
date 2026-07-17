"""collectors/ozon.py — Ozon финтранзакции → raw_ozon_transaction + разбор расходов.

Источник: POST /v3/finance/transaction/list (операции по дням, реал-тайм). Это аналог
raw_wb_report для WB. Официальный отчёт о реализации у Ozon месячный — нам он не нужен
для оперативной маржи, транзакции свежее.

- Грузим ВСЕ операции в raw_ozon_transaction (UPSERT по account+operation_id) — полное сырьё
  (payload JSONB), разбор по статьям делаем при построении витрины (НЕ при загрузке).
- РАЗБОР РАСХОДОВ без двойного счёта: Ozon отдаёт расход в двух перекрывающихся разрезах —
  amount операции и services[] (магистраль сидит ВНУТРИ нетто-операции «Доставка покупателю»).
  categorize_operation() раскладывает каждую операцию на категории по КОМПОНЕНТАМ
  (accruals_for_sale, sale_commission, services[].price, delivery_charge), а остаток
  сверяет с amount → Σ(категории) == amount, ничего не теряется и не дублируется.

Ключ связки Ozon↔МС (для будущей COGS-витрины) = первые 2 сегмента posting_number
`order_id-shipment`; окно МС широкое (−45д) под лаг выплат. Здесь пока только расходы.

Запуск:  ./venv/bin/python collectors/ozon.py 2026-06-01 2026-06-30 [oz_acc1]
"""
import os
import sys
import time
import pathlib

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
TXN_URL = "https://api-seller.ozon.ru/v3/finance/transaction/list"
# Ozon аутентификация — пара Client-Id + Api-Key на аккаунт.
CRED_ENV = {"oz_acc1": ("OZON_CLIENT_ID_ACC1", "OZON_API_KEY_ACC1"),
            "oz_acc2": ("OZON_CLIENT_ID_ACC2", "OZON_API_KEY_ACC2")}


def _headers(account):
    cid_env, key_env = CRED_ENV[account]
    cid, key = os.getenv(cid_env), os.getenv(key_env)
    if not cid or not key:
        raise RuntimeError(f"{cid_env}/{key_env} не заданы в .env")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


def _num(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------
# КЛАССИФИКАЦИЯ СТАТЕЙ РАСХОДОВ
# --------------------------------------------------------------------------
# Категории витрины. revenue (+), остальные — расходы (Ozon отдаёт со знаком −).
# points — баллы/Звёздные (партнёрская программа лояльности); partners — доставка/сеть
# Партнёров Ozon (realFBS + redistribution); compensation — возмещения «по вине Ozon»;
# fbo — складские операции FBO. В эталоне ЛК points+partners+acquiring сворачиваются в
# «Партнёрские программы», subscription — в «Рекламу»; храним раздельно (роллап — в отчёте).
CATEGORIES = ["revenue", "commission", "advertising", "logistics",
              "returns", "penalties", "acquiring", "storage", "subscription",
              "partners", "points", "compensation", "fbo", "other"]


def _classify_service(name):
    """services[].name (англ. машинные коды Ozon) → категория."""
    n = (name or "").lower()
    if "acquiring" in n:                       # в т.ч. MarketplaceRedistributionOfAcquiring
        return "acquiring"
    if "stars" in n:                           # ItemAgentServiceStarsMembership — Звёздные баллы
        return "points"
    if "redistribution" in n:                  # сеть Партнёров Ozon (last-mile/dropoff/returns)
        return "partners"
    if "premiummembership" in n:               # PremiumMembershipCommission — подписка
        return "subscription"
    if "return" in n:
        return "returns"
    if any(k in n for k in ("movement", "temporarystorage", "disposal",
                            "volumeweight", "cargoassortment")):
        return "fbo"                           # склад/вывоз/утилизация FBO
    if any(k in n for k in ("logistic", "lastmile", "dropoff", "handover",
                            "flow", "delivery", "courier")):
        return "logistics"
    if any(k in n for k in ("storage", "package", "processing", "assortment")):
        return "storage"
    return "other"


def _classify_optype(name):
    """operation_type_name (рус. название) → категория. Для standalone-операций
    (реклама/штрафы/подписка), у которых нет компонентов — статья из самого типа."""
    n = (name or "").lower()
    if "по вине ozon" in n or "потеря" in n:   # возмещения брак/потеря по вине площадки
        return "compensation"
    if "звёздн" in n or "звездн" in n:         # Звёздные товары — баллы
        return "points"
    if "партнёр" in n or "партнер" in n or "realfbs" in n:
        return "partners"
    if any(k in n for k in ("продвижен", "клик", "отзыв", "трафарет", "реклам")):
        return "advertising"
    if "эквайринг" in n:
        return "acquiring"
    if "подписк" in n or "premium" in n:
        return "subscription"
    if any(k in n for k in ("слот", "индекс ошибок", "жалоб", "просроч", "штраф", "нарушени")):
        return "penalties"
    if any(k in n for k in ("возврат", "отмен", "невыкуп")):
        return "returns"
    if any(k in n for k in ("размещени", "вывоз", "утилизац", "овх", "дополнительн")):
        return "fbo"
    if any(k in n for k in ("хранени", "упаковк", "обработк", "подготовк", "склад", "материал")):
        return "storage"
    if "перечислен" in n:                      # Перечисление за доставку от покупателя (+)
        return "other"
    if any(k in n for k in ("доставк", "курьер", "логистик", "магистрал", "выезд")):
        return "logistics"
    return "other"


def categorize_operation(op):
    """Раскладывает одну операцию по категориям так, что Σ(категории) == amount.

    Стратегия: считаем «отслеженные» компоненты (выручка, комиссия, доставка,
    services[]). Остаток amount − tracked: если компонентов не было (standalone —
    реклама/штраф/подписка) — вся сумма по типу операции; иначе мелкий остаток → other.
    Гарантирует отсутствие двойного счёта и сходимость к amount.
    """
    out = {c: 0.0 for c in CATEGORIES}
    accr = _num(op.get("accruals_for_sale"))
    comm = _num(op.get("sale_commission"))
    dch = _num(op.get("delivery_charge")) + _num(op.get("return_delivery_charge"))
    # Fix B: отрицательные accruals — это возвраты покупателя, не «минус-выручка».
    # Разводим по знаку: revenue — только продажи (+), returns — возвраты (−).
    out["returns" if accr < 0 else "revenue"] += accr
    out["commission"] += comm
    out["logistics"] += dch
    tracked = accr + comm + dch
    for s in (op.get("services") or []):
        p = _num(s.get("price"))
        out[_classify_service(s.get("name"))] += p
        tracked += p
    residual = _num(op.get("amount")) - tracked
    if abs(residual) > 0.005:
        if tracked == 0.0:
            out[_classify_optype(op.get("operation_type_name") or op.get("operation_type"))] += residual
        else:
            out["other"] += residual
    return out


# --------------------------------------------------------------------------
# СБОР
# --------------------------------------------------------------------------
def fetch_transactions(account, date_from, date_to):
    """Все операции за период, пагинация по page (page_size 1000). Обрабатываем 429."""
    H = _headers(account)
    out, page, pages = [], 1, 1
    while page <= pages:
        body = {"filter": {"date": {"from": f"{date_from}T00:00:00.000Z",
                                    "to": f"{date_to}T23:59:59.999Z"},
                           "posting_number": "", "transaction_type": "all"},
                "page": page, "page_size": 1000}
        r = requests.post(TXN_URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json()["result"]
        pages = res.get("page_count") or 1
        ops = res.get("operations") or []
        out.extend(ops)
        print(f"  [oz fetch] {date_from}..{date_to} стр.{page}/{pages} "
              f"+{len(ops)} (всего {len(out)})", flush=True)
        page += 1
        time.sleep(0.3)
    return out


PRODUCT_LIST_URL = "https://api-seller.ozon.ru/v3/product/list"
PRODUCT_INFO_URL = "https://api-seller.ozon.ru/v3/product/info/list"


def fetch_product_offer_map(account):
    """{sku(str): offer_id} — все sku товара (по источникам fbo/fbs/sds) → offer_id.

    offer_id Ozon = external_code МойСклад (проверено) → ключ fallback-COGS для FBO,
    у которых нет отгрузки в МС. Один offer_id несёт несколько sku (на источник)."""
    H = _headers(account)
    pids, last = [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H,
                          json={"filter": {"visibility": "ALL"}, "last_id": last, "limit": 1000},
                          timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        pids += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < 1000:
            break
    sku2offer = {}
    for i in range(0, len(pids), 1000):
        r = requests.post(PRODUCT_INFO_URL, headers=H, json={"product_id": pids[i:i + 1000]},
                          timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            r = requests.post(PRODUCT_INFO_URL, headers=H, json={"product_id": pids[i:i + 1000]},
                              timeout=120)
        r.raise_for_status()
        for it in r.json().get("items", []):
            offer = it.get("offer_id")
            if not offer:
                continue
            if it.get("sku"):
                sku2offer[str(it["sku"])] = offer
            for s in (it.get("sources") or []):
                if s.get("sku"):
                    sku2offer[str(s["sku"])] = offer
        time.sleep(0.3)
    print(f"  [oz products] {len(pids)} товаров → {len(sku2offer)} sku→offer_id", flush=True)
    return sku2offer


def load_raw(account, ops, date_from, date_to):
    recs = [{"account": account, "operation_id": o.get("operation_id"),
             "period_from": date_from, "period_to": date_to,
             "payload": psycopg2.extras.Json(o)}
            for o in ops if o.get("operation_id") is not None]
    return db.upsert("raw_ozon_transaction", recs, conflict_cols=["account", "operation_id"])


def main(date_from="2026-06-01", date_to="2026-06-30", account="oz_acc1"):
    print(f"Ozon {account} {date_from}..{date_to}", flush=True)
    ops = fetch_transactions(account, date_from, date_to)
    n_raw = load_raw(account, ops, date_from, date_to)
    # быстрый разбор по статьям для лога
    tot = {c: 0.0 for c in CATEGORIES}
    for o in ops:
        for c, v in categorize_operation(o).items():
            tot[c] += v
    print(f"Итого: операций {len(ops)} → raw {n_raw}", flush=True)
    for c in CATEGORIES:
        if abs(tot[c]) >= 1:
            print(f"  {c:<12} {tot[c]:>14,.0f}", flush=True)


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "2026-06-01",
         a[2] if len(a) > 2 else "2026-06-30",
         a[3] if len(a) > 3 else "oz_acc1")
