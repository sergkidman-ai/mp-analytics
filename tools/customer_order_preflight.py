# поток: fin — isolated read-only probes; no operational jobs or notifications.
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/reports/customer_orders_preflight_20261020"
OUT.mkdir(exist_ok=True, parents=True)
os.chmod(OUT, 0o700)
ENV = dotenv_values(ROOT / ".env")
MS = "https://api.moysklad.ru/api/remap/1.2/"
STATE = OUT / "evidence.json"
data = json.loads(STATE.read_text()) if STATE.exists() else {"calls": [], "captured_at": datetime.now(timezone.utc).isoformat()}
last = 0


def save():
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(STATE, 0o600)


def request(kind, path, *, params=None, body=None):
    global last
    assert len(data["calls"]) < 90, "preflight budget exhausted"
    time.sleep(max(0, 2.1 - (time.monotonic() - last)))
    if kind == "ms":
        assert not body and not path.startswith(("http", "/"))
        token = ENV.get("SHIPMENT_GUARD_MS_TOKEN")
        assert token, "dedicated token required"
        url = MS + path
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "gzip"}
        method = "GET"
    else:
        assert kind == "ozon"
        assert path in ("/v3/posting/fbs/list", "/v3/posting/fbs/get")
        url = "https://api-seller.ozon.ru" + path
        headers = {"Client-Id": ENV["OZON_CLIENT_ID_ACC2"], "Api-Key": ENV["OZON_API_KEY_ACC2"]}
        method = "POST"  # Ozon's read-only retrieval API.
    start = time.monotonic()
    last = start
    try:
        response = requests.request(method, url, headers=headers, params=params, json=body, timeout=25)
    except requests.RequestException as exc:
        data["calls"].append({"source": kind, "path": path, "error": type(exc).__name__})
        save()
        raise RuntimeError("read request failed; details redacted") from None
    item = {"source": kind, "path": path, "params": params, "http": response.status_code,
            "seconds": round(time.monotonic() - start, 3),
            "quota": {k: v for k, v in response.headers.items() if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}}
    data["calls"].append(item)
    save()
    if response.status_code != 200:
        try:
            error = response.json()
            item["error_codes"] = [x.get("code") for x in error.get("errors", [])]
            item["error_message"] = str(error.get("message", ""))[:250]
        except (ValueError, AttributeError):
            pass
        item["content_type"] = response.headers.get("Content-Type")
        item["server"] = response.headers.get("Server")
        save()
        print(json.dumps({"source": kind, "path": path, "http": response.status_code}))
        if response.status_code == 429:
            raise RuntimeError("rate limit: stopping preflight")
        return {}
    return response.json()


def ref(obj):
    return {k: obj[k] for k in ("id", "name", "code", "article", "externalCode", "meta") if k in obj}


def doc(obj):
    result = {k: obj[k] for k in ("id", "name", "code", "externalCode", "created", "updated", "moment", "applicable", "sum", "vatIncluded", "vatEnabled", "overhead") if k in obj}
    for key in ("organization", "agent", "store", "state", "rate"):
        if key in obj:
            result[key] = ref(obj[key]) if key != "rate" else obj[key]
    for key in ("demands", "purchaseOrders"):
        result[key] = obj.get(key, [])
    result["position_count"] = obj.get("positions", {}).get("meta", {}).get("size")
    return result


def product(obj):
    result = ref(obj)
    result["supplier"] = ref(obj.get("supplier", {}))
    return result


def positions(obj):
    return [{"id": p.get("id"), "quantity": p.get("quantity"), "price": p.get("price"),
             "reserve": p.get("reserve"), "discount": p.get("discount"), "overhead": p.get("overhead"),
             "assortment": product(p.get("assortment", {}))}
            for p in obj.get("rows", [])]


def posting(obj):
    return {k: obj[k] for k in ("posting_number", "order_id", "order_number", "status", "substatus", "in_process_at", "shipment_date", "products", "financial_data") if k in obj}


def initial():
    who = request("ms", "context/employee")
    data["identity"] = {"uid": who.get("uid"), "id": who.get("id")}
    assert who.get("uid") == "yunusov@dsquare", "unexpected dedicated user"
    for entity in ("organization", "store"):
        obj = request("ms", "entity/" + entity, params={"limit": 20})
        data[entity] = [ref(x) for x in obj.get("rows", [])]
    meta = request("ms", "entity/customerorder/metadata")
    data["states"] = [ref(x) for x in meta.get("states", [])]
    org = next(x for x in data["organization"] if "ДИСКВ" in x["name"])
    data["org_href"] = org["meta"]["href"]
    now = datetime.now(timezone.utc)
    for status in ("", "awaiting_packaging", "awaiting_deliver"):
        obj = request("ozon", "/v3/posting/fbs/list", body={
            "dir": "DESC", "filter": {"since": (now - timedelta(days=14)).isoformat(),
            "to": now.isoformat(), "status": status}, "limit": 20, "offset": 0,
            "with": {"financial_data": True}})
        result = obj.get("result") or {}
        data["ozon_" + (status or "recent")] = {
            "has_next": result.get("has_next"),
            "postings": [posting(p) for p in result.get("postings", [])]}
    obj = request("ms", "entity/customerorder", params={
        "filter": "organization=" + data["org_href"], "order": "updated,desc", "limit": 20,
        "expand": "state,store,agent"})
    data["recent_orders"] = [doc(x) for x in obj.get("rows", [])]
    data["recent_orders_total"] = obj.get("meta", {}).get("size")
    save()
    print(json.dumps({"identity": data["identity"], "states": [x.get("name") for x in data["states"]],
        "ozon": {key: {"count": len(data[key]["postings"]), "has_next": data[key]["has_next"]}
        for key in data if key.startswith("ozon_")},
        "recent_ms_status_counts": {s: sum(1 for x in data["recent_orders"] if x.get("state", {}).get("name") == s)
        for s in {x.get("state", {}).get("name") for x in data["recent_orders"]}},
        "calls": len(data["calls"])}, ensure_ascii=False))


def ms_orders():
    org = next(x for x in data["organization"] if "ДИСКВ" in x["name"])
    data["org_href"] = org["meta"]["href"]
    # Only the pilot's organization and known Ozon counterparties.
    base_filter = ("organization=" + data["org_href"]
                   + ";agent=" + MS + "entity/counterparty/b7e22828-0b7c-11ee-0a80-11480012ed47"
                   + ";agent=" + MS + "entity/counterparty/22240c7b-c88d-11ed-0a80-10e700065d1d")
    data["order_samples"] = {}
    for name in ("recent", "Подтвержден", "Неукомплектован", "Собирается"):
        flt = base_filter
        if name != "recent":
            state = next(x for x in data["states"] if x.get("name") == name)
            flt += ";state=" + state["meta"]["href"]
        obj = request("ms", "entity/customerorder", params={
            "filter": flt, "order": "updated,desc", "limit": 20 if name == "recent" else 3,
            "expand": "state,store,agent"})
        data["order_samples"][name] = {"total": obj.get("meta", {}).get("size"),
                                      "rows": [doc(x) for x in obj.get("rows", [])]}
        save()
    import psycopg2
    conn = psycopg2.connect(ENV["DATABASE_URL"], options="-c default_transaction_read_only=on -c statement_timeout=4000")
    with conn.cursor() as cur:
        cur.execute("SELECT payload,loaded_at FROM raw_ozon_posting WHERE account='oz_acc2' AND scheme='fbs' ORDER BY in_process_at DESC LIMIT 5")
        data["cached_ozon_samples"] = [{"posting": posting(p), "loaded_at": str(t)} for p, t in cur.fetchall()]
    conn.close()
    data["bridges"] = []
    for item in data["cached_ozon_samples"]:
        p = item["posting"]
        # Known historical bridge: order number without posting suffix.
        name = p["posting_number"].rsplit("-", 1)[0]
        obj = request("ms", "entity/customerorder", params={"filter": base_filter + ";name=" + name,
                      "limit": 10, "expand": "state,store,agent"})
        data["bridges"].append({"posting_number": p["posting_number"], "queried_name": name,
                               "total": obj.get("meta", {}).get("size"), "rows": [doc(x) for x in obj.get("rows", [])]})
        save()
    print(json.dumps({"samples": {k: {"total": v["total"],
          "orders": [{"name": x.get("name"), "state": x.get("state", {}).get("name"),
                      "store": x.get("store", {}).get("name"), "demands": len(x.get("demands", []))}
                     for x in v["rows"][:3]]} for k, v in data["order_samples"].items()},
          "bridges": [{"posting": b["posting_number"], "matches": b["total"]} for b in data["bridges"]],
          "calls": len(data["calls"])}, ensure_ascii=False))


def details():
    # Actual customerorder names retain the posting suffix; never infer absence
    # from the older demand bridge that sometimes omits it.
    data["exact_bridges"] = []
    for item in data["cached_ozon_samples"]:
        number = item["posting"]["posting_number"]
        obj = request("ms", "entity/customerorder", params={
            "filter": "organization=" + data["org_href"] + ";name=" + number,
            "limit": 10, "expand": "state,store,agent"})
        data["exact_bridges"].append({"posting_number": number, "total": obj.get("meta", {}).get("size"),
                                     "rows": [doc(x) for x in obj.get("rows", [])]})
        save()
    selected = data["order_samples"]["Неукомплектован"]["rows"][:2] + data["order_samples"]["Собирается"]["rows"][:1]
    shipped = next((x for x in data["order_samples"]["recent"]["rows"] if x.get("demands")), None)
    if shipped:
        selected.append(shipped)
    data["details"] = []
    remote = next(x for x in data["store"] if "Удаленный" in x["name"])
    for order in selected:
        evidence = {"order": order}
        obj = request("ms", f"entity/customerorder/{order['id']}/positions",
                      params={"limit": 20, "expand": "assortment"})
        evidence["positions"] = positions(obj)
        evidence["positions_total"] = obj.get("meta", {}).get("size")
        if order["state"]["name"] in ("Подтвержден", "Собирается"):
            evidence["preliminary_fifo"] = request("ms", "report/stock/byoperation",
                                                   params={"operation.id": order["id"]})
        if order["state"]["name"] == "Неукомплектован" and evidence["positions"]:
            p = evidence["positions"][0]["assortment"]
            href = p["meta"]["href"]
            assert href.startswith(MS + "entity/")
            evidence["product"] = product(request("ms", href[len(MS):], params={"expand": "supplier"}))
            enters = request("ms", "entity/enter", params={
                "filter": "store=" + remote["meta"]["href"] + ";assortment=" + href + ";applicable=true",
                "limit": 10, "expand": "store", "order": "updated,desc"})
            evidence["enters_total"] = enters.get("meta", {}).get("size")
            evidence["enters"] = [doc(x) for x in enters.get("rows", [])]
            if len(evidence["enters"]) == 1:
                eid = evidence["enters"][0]["id"]
                obj = request("ms", f"entity/enter/{eid}/positions",
                              params={"filter": "assortment=" + href, "limit": 20, "expand": "assortment"})
                evidence["enter_positions"] = positions(obj)
                evidence["enter_positions_total"] = obj.get("meta", {}).get("size")
        if order.get("demands"):
            href = order["demands"][0]["meta"]["href"]
            assert href.startswith(MS + "entity/demand/")
            demand = request("ms", href[len(MS):], params={"expand": "store"})
            evidence["demand"] = doc(demand)
            obj = request("ms", href[len(MS):] + "/positions", params={"limit": 20, "expand": "assortment"})
            evidence["demand_positions"] = positions(obj)
            evidence["actual_fifo"] = request("ms", "report/stock/byoperation",
                                              params={"operation.id": demand["id"]})
        data["details"].append(evidence)
        save()
    print(json.dumps({"exact_bridges": [{"posting": b["posting_number"], "matches": b["total"]}
           for b in data["exact_bridges"]],
           "details": [{"order": x["order"]["name"], "state": x["order"]["state"]["name"],
              "positions": x["positions_total"], "product_article": x.get("product", {}).get("article"),
              "enters": x.get("enters_total"), "enter_lines": x.get("enter_positions_total"),
              "has_fifo": bool(x.get("actual_fifo") or x.get("preliminary_fifo"))} for x in data["details"]],
           "calls": len(data["calls"])}, ensure_ascii=False))


def ozon_retry():
    now = datetime.now(timezone.utc)
    obj = request("ozon", "/v3/posting/fbs/list", body={
        "dir": "DESC", "filter": {"since": (now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00.000Z"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "status": ""}, "limit": 20, "offset": 0,
        "with": {"financial_data": True}})
    result = obj.get("result") or {}
    data["ozon_recent"] = {"has_next": result.get("has_next"),
                           "postings": [posting(p) for p in result.get("postings", [])]}
    save()
    print(json.dumps({"ozon_count": len(data["ozon_recent"]["postings"]),
                     "has_next": data["ozon_recent"]["has_next"]}))


def receipt_pages():
    # Positions do not support assortment filtering (412/1034).
    # Read the two identified 100-line receipts in bounded 20-line pages.
    for evidence in data["details"]:
        if not evidence.get("enters") or len(evidence["enters"]) != 1:
            continue
        receipt = evidence["enters"][0]
        count = receipt["position_count"]
        assert count <= 100, "bounded preflight receipt size"
        target_hrefs = {p["assortment"]["meta"]["href"] for p in evidence["positions"]}
        found = []
        for offset in range(0, count, 20):
            obj = request("ms", f"entity/enter/{receipt['id']}/positions",
                          params={"limit": 20, "offset": offset})
            found.extend(p for p in positions(obj) if p["assortment"]["meta"]["href"] in target_hrefs)
        evidence["matched_enter_positions"] = found
        evidence["receipt_coverage"] = {"matched": len(found), "order_lines": len(evidence["positions"])}
        evidence["receipt_price_total_rub"] = sum(
            p["quantity"] * next(e["price"] for e in found
                                if e["assortment"]["meta"]["href"] == p["assortment"]["meta"]["href"]) / 100
            for p in evidence["positions"]
            if any(e["assortment"]["meta"]["href"] == p["assortment"]["meta"]["href"] for e in found))
        save()
    print(json.dumps({"receipts": [{"order": e["order"]["name"], "coverage": e["receipt_coverage"],
        "cost_rub": e["receipt_price_total_rub"]} for e in data["details"] if "receipt_coverage" in e],
        "calls": len(data["calls"])}, ensure_ascii=False))


def ozon_one():
    number = data["cached_ozon_samples"][0]["posting"]["posting_number"]
    obj = request("ozon", "/v3/posting/fbs/get", body={
        "posting_number": number, "with": {"financial_data": True}})
    data["ozon_live_one"] = posting(obj.get("result") or {})
    save()
    print(json.dumps({"posting": data["ozon_live_one"].get("posting_number"),
                     "status": data["ozon_live_one"].get("status")}, ensure_ascii=False))


def final_links():
    number = data["ozon_live_one"]["posting_number"]
    order = next(b["rows"][0] for b in data["exact_bridges"] if b["posting_number"] == number)
    evidence = {"order": order}
    obj = request("ms", f"entity/customerorder/{order['id']}/positions",
                  params={"limit": 20, "expand": "assortment"})
    evidence["positions"] = positions(obj)
    href = order["demands"][0]["meta"]["href"]
    assert href.startswith(MS + "entity/demand/")
    demand = request("ms", href[len(MS):], params={"expand": "store"})
    evidence["demand"] = doc(demand)
    obj = request("ms", href[len(MS):] + "/positions", params={"limit": 20, "expand": "assortment"})
    evidence["demand_positions"] = positions(obj)
    evidence["actual_fifo"] = request("ms", "report/stock/byoperation", params={"operation.id": demand["id"]})
    data["live_chain"] = evidence
    save()
    data["changed_order_counts"] = {}
    now = datetime.now(timezone(timedelta(hours=3)))
    for minutes in (10, 1440):
        since = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        flt = ("organization=" + data["org_href"]
               + ";agent=" + MS + "entity/counterparty/b7e22828-0b7c-11ee-0a80-11480012ed47"
               + ";agent=" + MS + "entity/counterparty/22240c7b-c88d-11ed-0a80-10e700065d1d"
               + ";updated>=" + since)
        obj = request("ms", "entity/customerorder", params={"filter": flt, "limit": 1})
        data["changed_order_counts"][str(minutes)] = obj.get("meta", {}).get("size")
        save()
    print(json.dumps({"posting": number,
        "actual_fifo_rub": sum(p.get("cost", 0) for r in evidence["actual_fifo"].get("rows", []) for p in r.get("positions", []))/100,
        "changed_order_counts": data["changed_order_counts"], "calls": len(data["calls"])}, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1] == "initial":
        initial()
    elif sys.argv[1] == "ms_orders":
        ms_orders()
    elif sys.argv[1] == "details":
        details()
    elif sys.argv[1] == "ozon_retry":
        ozon_retry()
    elif sys.argv[1] == "receipt_pages":
        receipt_pages()
    elif sys.argv[1] == "ozon_one":
        ozon_one()
    elif sys.argv[1] == "final_links":
        final_links()
