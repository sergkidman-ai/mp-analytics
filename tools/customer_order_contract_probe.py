# поток: fin — bounded read-only evidence for the customer-order contract.
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/reports/customer_order_contract_20261020"
OUT.mkdir(parents=True, exist_ok=True)
os.chmod(OUT, 0o700)
FILE = OUT / "evidence.json"
state = json.loads(FILE.read_text()) if FILE.exists() else {"calls": [], "pages": []}
env = dotenv_values(ROOT / ".env")
MS = "https://api.moysklad.ru/api/remap/1.2/"
last = 0


def save():
    FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(FILE, 0o600)


def call(source, path, *, body=None, params=None):
    global last
    assert len(state["calls"]) < 45, "total probe budget"
    pause = 10 if source == "ozon" else 2.1
    time.sleep(max(0, pause - (time.monotonic() - last)))
    if source == "ozon":
        assert path in ("/v4/posting/fbs/list", "/v3/posting/fbs/get")
        url = "https://api-seller.ozon.ru" + path
        method = "POST"
        headers = {"Client-Id": env["OZON_CLIENT_ID_ACC2"], "Api-Key": env["OZON_API_KEY_ACC2"]}
    else:
        assert source == "ms" and not body and not path.startswith(("http", "/"))
        token = env.get("SHIPMENT_GUARD_MS_TOKEN")
        assert token, "dedicated MS token required"
        url, method = MS + path, "GET"
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "gzip"}
    started = time.monotonic()
    last = started
    item = {"at": datetime.now(timezone.utc).isoformat(), "source": source,
            "path": path, "body": body, "params": params}
    try:
        r = requests.request(method, url, headers=headers, json=body, params=params, timeout=25)
    except requests.RequestException as exc:
        item["error"] = type(exc).__name__
        state["calls"].append(item)
        save()
        raise RuntimeError("probe failed; request details redacted") from None
    item.update(http=r.status_code, seconds=round(time.monotonic() - started, 3),
                retry_after=r.headers.get("Retry-After"))
    state["calls"].append(item)
    save()
    if r.status_code != 200:
        try:
            obj = r.json()
            item["error_message"] = str(obj.get("message", ""))[:200]
            item["error_codes"] = [e.get("code") for e in obj.get("errors", [])]
        except ValueError:
            pass
        save()
        raise RuntimeError(f"HTTP {r.status_code}; no automatic retry")
    return r.json()


def post(p):
    fields = ("posting_number", "order_id", "order_number", "status", "substatus",
              "in_process_at", "shipment_date")
    result = {k: p[k] for k in fields if k in p}
    result["products"] = [{k: x[k] for k in
                           ("offer_id", "sku", "name", "quantity", "price", "currency_code")
                           if k in x} for x in p.get("products", [])]
    if "financial_data" in p:
        result["financial_products"] = p["financial_data"].get("products", [])
    return result


def collect():
    assert not state["pages"], "snapshot already started; do not duplicate"
    now = datetime.now(timezone.utc)
    body = {"filter": {"since": "2026-10-19T00:00:00Z",
                      "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "limit": 20, "cursor": "", "with": {"financial_data": True}}
    state["snapshot_body"] = body.copy()
    seen_cursors = set()
    for page in range(6):
        obj = call("ozon", "/v4/posting/fbs/list", body=body.copy())
        assert isinstance(obj.get("postings"), list) and isinstance(obj.get("has_next"), bool), "unexpected v4 envelope"
        state["pages"].append({"postings": [post(p) for p in obj["postings"]],
                               "has_next": obj["has_next"], "cursor": obj.get("cursor")})
        save()
        if not obj["has_next"]:
            state["snapshot_complete"] = True
            break
        cursor = obj.get("cursor")
        assert cursor and cursor not in seen_cursors, "empty/repeated cursor"
        seen_cursors.add(cursor)
        body["cursor"] = cursor
    save()
    rows = [p for page in state["pages"] for p in page["postings"]]
    print(json.dumps({"pages": len(state["pages"]), "rows": len(rows),
        "unique": len({p["posting_number"] for p in rows}), "complete": state.get("snapshot_complete", False),
        "statuses": {s: sum(p["status"] == s for p in rows) for s in sorted({p["status"] for p in rows})}}, ensure_ascii=False))


def filter_probe():
    body = {**state["snapshot_body"], "limit": 10, "filter": {
        **state["snapshot_body"]["filter"], "statuses": ["awaiting_packaging", "awaiting_deliver"]},
        "sort_dir": "DESC"}
    obj = call("ozon", "/v4/posting/fbs/list", body=body)
    state["filter_probe"] = {"body": body, "postings": [post(p) for p in obj.get("postings", [])],
                             "has_next": obj.get("has_next"), "cursor": obj.get("cursor")}
    save()
    print(json.dumps({"actual_statuses": sorted({p["status"] for p in state["filter_probe"]["postings"]}),
                     "count": len(state["filter_probe"]["postings"])}, ensure_ascii=False))


def ms_match():
    prior = json.loads((ROOT / "docs/reports/customer_orders_preflight_20261020/evidence.json").read_text())
    who = call("ms", "context/employee")
    assert who.get("uid") == "yunusov@dsquare"
    state["ms_uid"] = who["uid"]
    posts = [p for page in state["pages"] for p in page["postings"]]
    numbers = [p["posting_number"] for p in posts]
    numbers += ["0177163975-0149-1", "0177163975-0149-2"]
    base = ("organization=" + prior["org_href"]
            + ";agent=" + MS + "entity/counterparty/b7e22828-0b7c-11ee-0a80-11480012ed47"
            + ";agent=" + MS + "entity/counterparty/22240c7b-c88d-11ed-0a80-10e700065d1d")
    state["ms_orders"] = []
    for start in range(0, len(numbers), 18):
        group = numbers[start:start+18]
        flt = base + "".join(";name=" + n for n in group)
        obj = call("ms", "entity/customerorder", params={"filter": flt, "limit": 20,
                   "expand": "state,store,agent"})
        assert obj.get("meta", {}).get("size", 0) <= 20, "ambiguous batch: pagination required"
        for x in obj.get("rows", []):
            assert x["name"] in group, "name filter not respected"
            d = {k: x[k] for k in ("id", "name", "created", "updated", "sum", "applicable") if k in x}
            for k in ("state", "store", "agent", "organization"):
                d[k] = {key: x.get(k, {}).get(key) for key in ("id", "name", "meta")}
            d["demands"] = x.get("demands", [])
            d["position_count"] = x.get("positions", {}).get("meta", {}).get("size")
            state["ms_orders"].append(d)
        save()
    counts = {n: sum(d["name"] == n for d in state["ms_orders"]) for n in numbers}
    state["ms_match_summary"] = {
        "snapshot_posts": len(posts), "matched_snapshot": sum(counts[n] == 1 for n in numbers[:len(posts)]),
        "missing": [n for n in numbers if counts[n] == 0],
        "ambiguous": [n for n in numbers if counts[n] > 1],
        "split_examples": {n: counts[n] for n in numbers[len(posts):]}}
    save()
    print(json.dumps(state["ms_match_summary"], ensure_ascii=False))


def samples():
    by = {d["name"]: d for d in state["ms_orders"]}
    numbers = ["71295564-0105-1", "45473453-0495-1", "0177163975-0149-1", "0177163975-0149-2"]
    confirmed = next((d["name"] for d in state["ms_orders"] if d["state"]["name"] == "Подтвержден"), None)
    if confirmed:
        numbers.append(confirmed)
    posts = {p["posting_number"]: p for page in state["pages"] for p in page["postings"]}
    state["samples"] = []
    for n in numbers:
        e = {"number": n, "ms_order": by[n]}
        if n in ("71295564-0105-1", "0177163975-0149-1", "0177163975-0149-2"):
            obj = call("ozon", "/v3/posting/fbs/get", body={"posting_number": n, "with": {"financial_data": True}})
            e["ozon_get"] = post(obj["result"])
        e["ozon_list"] = posts.get(n)
        obj = call("ms", f"entity/customerorder/{by[n]['id']}/positions",
                   params={"limit": 20, "expand": "assortment"})
        assert obj.get("meta", {}).get("size", 0) <= 20
        e["ms_positions"] = [
            {**{k: p.get(k) for k in ("id", "quantity", "price", "discount", "reserve")},
             "assortment": {k: p.get("assortment", {}).get(k) for k in
                            ("id", "meta", "code", "externalCode", "article")}}
            for p in obj.get("rows", [])]
        if n == confirmed:
            e["preliminary_fifo"] = call("ms", "report/stock/byoperation", params={"operation.id": by[n]["id"]})
        state["samples"].append(e)
        save()
    import psycopg2
    conn = psycopg2.connect(env["DATABASE_URL"], options="-c default_transaction_read_only=on -c statement_timeout=4000")
    with conn.cursor() as cur:
        cur.execute("SELECT external_code,components,resolved_at FROM set_cost WHERE external_code=%s", ("2306",))
        state["sample_bom"] = [{"code": r[0], "components": r[1], "resolved_at": str(r[2])} for r in cur.fetchall()]
    conn.close()
    save()
    print(json.dumps({"samples": [{"number": e["number"], "state": e["ms_order"]["state"]["name"],
          "ms_lines": [{"external_code": p["assortment"].get("externalCode"), "quantity": p["quantity"]}
                       for p in e["ms_positions"]]} for e in state["samples"]],
          "bom": state["sample_bom"]}, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1] == "collect":
        collect()
    elif sys.argv[1] == "filter_probe":
        filter_probe()
    elif sys.argv[1] == "ms_match":
        ms_match()
    elif sys.argv[1] == "samples":
        samples()
