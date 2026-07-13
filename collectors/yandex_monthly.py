"""collectors/yandex_monthly.py — Яндекс.Маркет: история заказов + экономика по месяцам.

Бизнес-эндпоинт /orders отдаёт ~30 дней. Для ИСТОРИИ используем /campaigns/{id}/stats/orders
(принимает dateFrom/dateTo, отдаёт месяцы назад). В заказе — вся экономика:
payments (заплатил покупатель), subsidies (доплата Маркета), commissions[] по типам
(FEE=комиссия, DELIVERY_*=логистика, PAYMENT_TRANSFER=эквайринг, AUCTION_PROMOTION=буст-реклама,
AGENCY=агентское), статусы (RETURNED и т.п.), items.shopSku (наш артикул).

Пишем: сырьё → raw_yandex_stats_order; агрегаты → yandex_monthly (совместимость)
и yandex_finance_monthly (выручка/расходы/возвраты/COGS по месяцам).
Выручка = Σ payments без CANCELLED — сходится с учётной таблицей.

Запуск:  ./venv/bin/python collectors/yandex_monthly.py [YYYY-MM-01 since]
"""
import os
import sys
import time
import datetime
import pathlib
from collections import defaultdict, Counter

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"

RETURN_STATUSES = {"RETURNED", "PARTIALLY_RETURNED"}
COMM_COL = {"FEE": "fee", "PAYMENT_TRANSFER": "transfer",
            "AUCTION_PROMOTION": "promotion", "AGENCY": "agency"}


def _cfg():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    if not key or not camps:
        raise RuntimeError("YANDEX_API_KEY_ACC1 / YANDEX_CAMPAIGN_ID_ACC1 не заданы")
    return key, camps


def _comm_col(ctype):
    if ctype in COMM_COL:
        return COMM_COL[ctype]
    if "DELIVERY" in (ctype or ""):
        return "delivery"
    return "other_fee"


def _pay_sum(o):
    """Деньги покупателя по заказу: PAYMENT − REFUND (у возврата REFUND идёт с плюсом!)."""
    pay = refund = 0.0
    for p in (o.get("payments") or []):
        if p.get("type") == "REFUND":
            refund += p.get("total", 0) or 0
        else:
            pay += p.get("total", 0) or 0
    return pay - refund, refund


def collect(since="2026-01-01"):
    key, camps = _cfg()
    H = {"Api-Key": key, "Content-Type": "application/json"}
    today = datetime.date.today().isoformat()
    # Себест: yandex_cost (средняя по МС-заказам Маркета) + фолбэк products.cost_seb по external_code
    cost = {r["external_code"]: float(r["c"]) for r in db.query(
        """SELECT external_code, min(cost_seb) c FROM products
           WHERE external_code IS NOT NULL AND cost_seb>0 GROUP BY 1""")}
    cost.update({r["offer"]: float(r["cost_per_unit"]) for r in db.query(
        "SELECT offer, cost_per_unit FROM yandex_cost WHERE offer NOT LIKE '\\_\\_%%' AND cost_per_unit>0")})
    agg = defaultdict(lambda: {"revenue": 0.0, "subsidy": 0.0, "orders": 0})
    fin = defaultdict(lambda: defaultdict(float))
    comm_types = Counter()
    raw_buf, n_raw = [], 0
    for cid in camps:
        tok = None
        for _ in range(200):
            params = {"page_token": tok} if tok else {}
            r = requests.post(f"{API}/campaigns/{cid}/stats/orders", headers=H, params=params,
                              json={"dateFrom": since, "dateTo": today}, timeout=120)
            if r.status_code != 200:
                print(f"  [ya monthly] cid {cid}: HTTP {r.status_code} — стоп", flush=True)
                break
            res = r.json().get("result", {})
            for o in res.get("orders", []):
                oid = o.get("id")
                if oid is not None:
                    raw_buf.append({"account": ACCOUNT, "order_id": str(oid),
                                    "campaign_id": str(cid),
                                    "payload": psycopg2.extras.Json(o)})
                if o.get("status") == "CANCELLED":
                    continue
                cd = (o.get("creationDate") or "")[:7]   # YYYY-MM
                if len(cd) != 7:
                    continue
                mo = cd + "-01"
                a = agg[mo]
                a["orders"] += 1
                pay, refund = _pay_sum(o)
                sub = sum(s.get("amount", 0) or 0 for s in (o.get("subsidies") or []))
                a["revenue"] += pay
                a["subsidy"] += sub
                f = fin[mo]
                f["revenue"] += pay
                f["subsidy"] += sub
                f["orders"] += 1
                if o.get("status") in RETURN_STATUSES:
                    f["returns_orders"] += 1
                f["returns_sum"] += refund
                for c in (o.get("commissions") or []):
                    comm_types[c.get("type")] += 1
                    f[_comm_col(c.get("type"))] += c.get("actual", 0) or 0
                # COGS: без отмен и возвратов (товар вернулся — себест не списываем)
                if o.get("status") not in RETURN_STATUSES:
                    for it in (o.get("items") or []):
                        q = it.get("count", 0) or 0
                        sku = str(it.get("shopSku") or "")
                        f["qty"] += q
                        if sku in cost:
                            f["cogs"] += cost[sku] * q
                            f["qty_cov"] += q
            if len(raw_buf) >= 500:
                n_raw += db.upsert("raw_yandex_stats_order", raw_buf,
                                   conflict_cols=["account", "order_id"],
                                   update_cols=["campaign_id", "payload"])
                raw_buf = []
            tok = (res.get("paging") or {}).get("nextPageToken")
            if not tok:
                break
            time.sleep(0.5)
    if raw_buf:
        n_raw += db.upsert("raw_yandex_stats_order", raw_buf,
                           conflict_cols=["account", "order_id"],
                           update_cols=["campaign_id", "payload"])
    recs = [{"account": ACCOUNT, "month": mo, "revenue": round(v["revenue"], 2),
             "subsidy": round(v["subsidy"], 2), "orders": v["orders"]}
            for mo, v in sorted(agg.items())]
    if recs:
        db.upsert("yandex_monthly", recs, conflict_cols=["account", "month"],
                  update_cols=["revenue", "subsidy", "orders"])
    frecs = []
    for mo, f in sorted(fin.items()):
        frecs.append({"account": ACCOUNT, "month": mo,
                      "revenue": round(f["revenue"], 2), "subsidy": round(f["subsidy"], 2),
                      "orders": int(f["orders"]),
                      "returns_orders": int(f["returns_orders"]), "returns_sum": round(f["returns_sum"], 2),
                      "fee": round(f["fee"], 2), "delivery": round(f["delivery"], 2),
                      "transfer": round(f["transfer"], 2), "promotion": round(f["promotion"], 2),
                      "agency": round(f["agency"], 2), "other_fee": round(f["other_fee"], 2),
                      # непокрытые штуки — импутация по средней себест покрытых (как FBO у ВБ)
                      "cogs": round(f["cogs"] + (f["qty"] - f["qty_cov"]) * (f["cogs"] / f["qty_cov"])
                                    if f["qty_cov"] else f["cogs"], 2),
                      "cogs_cov_pct": round(f["qty_cov"] / f["qty"] * 100, 1) if f["qty"] else 0})
    if frecs:
        db.upsert("yandex_finance_monthly", frecs, conflict_cols=["account", "month"],
                  update_cols=["revenue", "subsidy", "orders", "returns_orders", "returns_sum",
                               "fee", "delivery", "transfer", "promotion", "agency", "other_fee",
                               "cogs", "cogs_cov_pct"])
        db.execute("UPDATE yandex_finance_monthly SET updated_at=now() WHERE account=%s", (ACCOUNT,))
    for r in frecs:
        mp = r["fee"] + r["delivery"] + r["transfer"] + r["promotion"] + r["agency"] + r["other_fee"]
        print(f"  {r['month'][:7]}: выручка {r['revenue']:,.0f} | субсидия {r['subsidy']:,.0f} | "
              f"заказов {r['orders']} | возвратов {r['returns_orders']} ({r['returns_sum']:,.0f}) | "
              f"расходы МП {mp:,.0f} (комиссия {r['fee']:,.0f}, логистика {r['delivery']:,.0f}, "
              f"эквайринг {r['transfer']:,.0f}, буст {r['promotion']:,.0f}) | "
              f"COGS {r['cogs']:,.0f} ({r['cogs_cov_pct']:.0f}%)", flush=True)
    print(f"Яндекс.Маркет: сырья {n_raw} заказов, помесячно {len(frecs)} месяцев | "
          f"типы commissions: {dict(comm_types)}", flush=True)


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    print(f"Яндекс.Маркет помесячно с {since} (stats/orders)", flush=True)
    collect(since)


if __name__ == "__main__":
    main()
