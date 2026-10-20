# поток: fin — bounded retrieval diagnostics, no service/API mutations.
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
out = root / "docs/reports/customer_orders_preflight_20261020"
state = out / "ozon_diagnostic.json"
history = json.loads(state.read_text()) if state.exists() else []
assert len(history) < 6, "diagnostic request budget"
mode = sys.argv[1]
assert mode in ("list_basic", "list_financial", "unfulfilled", "get", "v4", "v4_next", "v4_active")
now = datetime.now(timezone.utc)
if history:
    previous = history[-1]
    elapsed = (now - datetime.fromisoformat(previous["at"])).total_seconds()
    pause = max(15, float(previous.get("retry_after") or 0)) - elapsed
    if pause > 0:
        raise SystemExit("Wait until the recorded cooldown has elapsed.")
v = dotenv_values(root / ".env")
start_date = (now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00.000Z")
end_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
body = {"dir": "DESC", "filter": {"since": start_date, "to": end_date, "status": ""},
        "limit": 10, "offset": 0, "with": {"financial_data": mode == "list_financial"}}
path = "/v3/posting/fbs/list"
if mode.startswith("v4"):
    path = "/v4/posting/fbs/list"
    body = {"sort_dir": "desc", "filter": {"since": start_date, "to": end_date},
            "limit": 10, "cursor": "", "with": {"financial_data": True}}
    if mode == "v4_next":
        prior = next(x for x in reversed(history) if x["mode"] == "v4" and x.get("http") == 200)
        body = {**prior["body"], "cursor": prior["cursor"]}
    if mode == "v4_active":
        body["filter"]["status"] = ["awaiting_packaging", "awaiting_deliver"]
if mode == "unfulfilled":
    path = "/v3/posting/fbs/unfulfilled/list"
    body["filter"] = {"cutoff_from": start_date, "cutoff_to": end_date, "status": ""}
if mode == "get":
    path = "/v3/posting/fbs/get"
    body = {"posting_number": "71295564-0105-1", "with": {"financial_data": True}}
item = {"at": now.isoformat(), "mode": mode, "path": path, "body": body}
begin = time.monotonic()
try:
    r = requests.post("https://api-seller.ozon.ru" + path,
                      headers={"Client-Id": v["OZON_CLIENT_ID_ACC2"],
                               "Api-Key": v["OZON_API_KEY_ACC2"],
                               "Content-Type": "application/json"},
                      json=body, timeout=25)
    item.update(http=r.status_code, seconds=round(time.monotonic() - begin, 3),
                retry_after=r.headers.get("Retry-After"),
                trace_headers={k: val for k, val in r.headers.items()
                               if any(s in k.lower() for s in ("request-id", "trace-id", "ratelimit"))})
    try:
        obj = r.json()
    except ValueError:
        obj = {}
    if r.status_code == 200:
        result = obj if mode.startswith("v4") else (obj.get("result") or {})
        postings = result.get("postings", []) if mode != "get" else [result]
        item["has_next"] = result.get("has_next")
        item["cursor"] = result.get("cursor")
        item["count"] = len(postings)
        item["postings"] = [{k: p[k] for k in
                            ("posting_number", "status", "substatus", "products", "in_process_at", "shipment_date")
                            if k in p} for p in postings]
    else:
        item["error_code"] = obj.get("code")
        item["error_message"] = str(obj.get("message", ""))[:250]
except requests.RequestException as exc:
    item["transport_error"] = type(exc).__name__
history.append(item)
state.write_text(json.dumps(history, ensure_ascii=False, indent=2))
os.chmod(state, 0o600)
print(json.dumps({k: v for k, v in item.items() if k not in ("body", "postings")}, ensure_ascii=False))
