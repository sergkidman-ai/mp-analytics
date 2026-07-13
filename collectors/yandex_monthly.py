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


def collect_offers():
    """Каталог offer-mappings (бизнес-уровень) → raw_yandex_offer: баркоды, закупочная, marketSku."""
    key = os.getenv("YANDEX_API_KEY_ACC1")
    biz = os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    buf, tok, n = [], None, 0
    for _ in range(200):
        params = {"limit": 200}
        if tok:
            params["page_token"] = tok
        r = requests.post(f"{API}/v2/businesses/{biz}/offer-mappings", headers=H, params=params,
                          json={}, timeout=90)
        r.raise_for_status()
        res = r.json().get("result", {})
        for om in res.get("offerMappings", []):
            o = om.get("offer") or {}
            if o.get("offerId"):
                buf.append({"account": ACCOUNT, "offer_id": str(o["offerId"]),
                            "payload": psycopg2.extras.Json(om)})
        if len(buf) >= 1000:
            n += db.upsert("raw_yandex_offer", buf, conflict_cols=["account", "offer_id"],
                           update_cols=["payload"])
            buf = []
        tok = (res.get("paging") or {}).get("nextPageToken")
        if not tok:
            break
        time.sleep(0.3)
    if buf:
        n += db.upsert("raw_yandex_offer", buf, conflict_cols=["account", "offer_id"],
                       update_cols=["payload"])
    print(f"  каталог офферов: {n} записано", flush=True)
    return n


def _ms_cogs_monthly(since="2026-01-01"):
    """ФАКТ себеста Маркета по месяцам из МС-заказов «Покупатель Маркет»/«Я.Маркет Экспресс»:
    Σ products.cost_seb × qty по позициям, месяц = moment заказа. Это те же продажи, что в
    stats/orders (сверка ~600 API ≈ 587 МС за месяц), поэтому покрытие фактом ~100%."""
    tok = os.getenv("MOYSKLAD_TOKEN")
    if not tok:
        return {}
    H = {"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"}
    MS = "https://api.moysklad.ru/api/remap/1.2"
    cost = {r["ms_id"]: float(r["cost_seb"] or 0) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}
    out = defaultdict(float)
    for name in ("Покупатель Маркет", "Я.Маркет Экспресс"):
        rr = requests.get(f"{MS}/entity/counterparty", headers=H,
                          params={"filter": f"name={name}"}, timeout=60).json().get("rows", [])
        if not rr:
            continue
        href = rr[0]["meta"]["href"]
        offset = 0
        while True:
            r = requests.get(f"{MS}/entity/customerorder", headers=H, timeout=90, params={
                "filter": f"agent={href};moment>={since} 00:00:00", "limit": 100, "offset": offset,
                "expand": "positions.assortment"})
            rows = r.json().get("rows", [])
            if not rows:
                break
            for o in rows:
                mo = (o.get("moment") or "")[:7]
                if len(mo) != 7:
                    continue
                for p in (o.get("positions") or {}).get("rows", []):
                    a = p.get("assortment") or {}
                    msid = a.get("id") or a.get("meta", {}).get("href", "").split("/")[-1].split("?")[0]
                    out[mo + "-01"] += cost.get(msid, 0.0) * (p.get("quantity", 0) or 0)
            offset += 100
            if len(rows) < 100:
                break
    return dict(out)


def _cost_map():
    """Себест по offerId (=shopSku), цепочка: yandex_cost (факт МС-заказов Маркета) →
    products.cost_seb по external_code → баркод оффера→ms_barcode→МС (cost_seb, потом buy_price) →
    закупочная из карточки ЯМ (purchasePrice). Возвращает {sku: (cost, источник)}."""
    ext = {r["external_code"]: float(r["c"]) for r in db.query(
        """SELECT external_code, min(cost_seb) c FROM products
           WHERE external_code IS NOT NULL AND cost_seb>0 GROUP BY 1""")}
    yc = {r["offer"]: float(r["cost_per_unit"]) for r in db.query(
        "SELECT offer, cost_per_unit FROM yandex_cost WHERE offer NOT LIKE '\\_\\_%%' AND cost_per_unit>0")}
    bc2ms = {r["barcode"]: r["ms_id"] for r in db.query("SELECT barcode, ms_id FROM ms_barcode")}
    seb_ms = {r["ms_id"]: float(r["cost_seb"]) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}
    buy_ms = {r["ms_id"]: float(r["buy_price"]) for r in db.query(
        "SELECT ms_id, buy_price FROM ms_product WHERE buy_price>0")}
    out = {}
    for sku, c in ext.items():
        out[sku] = (c, "ext")
    for sku, c in yc.items():
        out[sku] = (c, "yc")
    for r in db.query("SELECT offer_id, payload FROM raw_yandex_offer WHERE account=%s", (ACCOUNT,)):
        sku = r["offer_id"]
        if sku in out:
            continue
        o = (r["payload"] or {}).get("offer") or {}
        msids = [bc2ms[b] for b in (o.get("barcodes") or []) if b in bc2ms]
        cs = [seb_ms[m] for m in msids if m in seb_ms]
        bs = [buy_ms[m] for m in msids if m in buy_ms]
        pp = float((o.get("purchasePrice") or {}).get("value") or 0)
        if cs:
            out[sku] = (min(cs), "bc")
        elif bs:
            out[sku] = (min(bs), "bc")
        elif pp > 0:
            out[sku] = (pp, "pp")
    return out


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
    # Себест: цепочка yandex_cost → external_code → баркод → закупочная ЯМ (см. _cost_map)
    cmap = _cost_map()
    cost = {sku: c for sku, (c, _src) in cmap.items()}
    src_cnt = Counter(src for _, src in cmap.values())
    print(f"  карта себеста: {len(cost)} SKU ({dict(src_cnt)})", flush=True)
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
    # COGS: приоритет — ФАКТ из МС-заказов Маркета за месяц; фолбэк — карта по SKU с импутацией
    ms_fact = {}
    try:
        ms_fact = _ms_cogs_monthly(since)
    except Exception as e:  # МС недоступен — работаем по карте
        print(f"  [ya monthly] МС-факт себеста недоступен: {e}", flush=True)
    frecs = []
    for mo, f in sorted(fin.items()):
        map_cogs = round(f["cogs"] + (f["qty"] - f["qty_cov"]) * (f["cogs"] / f["qty_cov"])
                         if f["qty_cov"] else f["cogs"], 2)
        fact = ms_fact.get(mo)
        frecs.append({"account": ACCOUNT, "month": mo,
                      "revenue": round(f["revenue"], 2), "subsidy": round(f["subsidy"], 2),
                      "orders": int(f["orders"]),
                      "returns_orders": int(f["returns_orders"]), "returns_sum": round(f["returns_sum"], 2),
                      "fee": round(f["fee"], 2), "delivery": round(f["delivery"], 2),
                      "transfer": round(f["transfer"], 2), "promotion": round(f["promotion"], 2),
                      "agency": round(f["agency"], 2), "other_fee": round(f["other_fee"], 2),
                      "cogs": round(fact, 2) if fact else map_cogs,
                      "cogs_cov_pct": 100.0 if fact else (
                          round(f["qty_cov"] / f["qty"] * 100, 1) if f["qty"] else 0)})
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
    collect_offers()
    collect(since)


if __name__ == "__main__":
    main()
