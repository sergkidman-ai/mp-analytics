# поток: fin
"""Timestamped WB FBO observations for the acc1 pilot; no MoySklad access."""
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from psycopg2.extras import Json, execute_values

from collectors.wb import _token
from core import db

URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses"
LOCK_ID = 7348201791
ACCOUNT = "wb_acc1"
PAGE_LIMIT = 5000


def _whole(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid_{name}")
    return value


def normalize(items):
    """Keep the WB row as evidence and reject incomplete/duplicate stock rows."""
    if not isinstance(items, list) or not items:
        raise ValueError("empty_wb_inventory")
    rows = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_wb_inventory_item")
        nm = _whole(item.get("nmId"), "nm_id")
        chrt = _whole(item.get("chrtId"), "chrt_id")
        warehouse_id = _whole(item.get("warehouseId"), "warehouse_id")
        warehouse_name = item.get("warehouseName")
        if nm <= 0 or chrt <= 0 or not isinstance(warehouse_name, str) or not warehouse_name:
            raise ValueError("invalid_wb_inventory_identity")
        key = (nm, chrt, warehouse_id)
        if key in seen:
            raise ValueError("duplicate_wb_inventory_row")
        seen.add(key)
        numbers = []
        for field in ("quantity", "inWayToClient", "inWayFromClient"):
            value = _whole(item.get(field), field)
            if value < 0:
                raise ValueError(f"negative_{field}")
            numbers.append(Decimal(value))
        rows.append((nm, chrt, warehouse_id, warehouse_name,
                     item.get("regionName"), *numbers, Json(item)))
    return rows


def fetch(session, token):
    started = datetime.now(timezone.utc)
    items = []
    offset = 0
    while True:
        response = session.post(URL, headers={"Authorization": token},
                                json={"limit": PAGE_LIMIT, "offset": offset}, timeout=90)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"wb_inventory_http_{response.status_code}")
        data = response.json().get("data")
        page = data.get("items") if isinstance(data, dict) else None
        if not isinstance(page, list):
            raise ValueError("invalid_wb_inventory_response")
        items.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(20)  # WB limit: one request per 20 seconds per seller account.
    return normalize(items), started, datetime.now(timezone.utc)


def collect(account=ACCOUNT, session=None):
    if account != ACCOUNT:
        raise ValueError("wb_intraday_pilot_acc1_only")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_ID,))
            if not cur.fetchone()[0]:
                return {"busy": True}
            cur.execute("SELECT max(completed_at) FROM wb_fbo_intraday_capture WHERE account=%s",
                        (account,))
            latest = cur.fetchone()[0]
            if latest and latest > datetime.now(timezone.utc) - timedelta(minutes=90):
                return {"skipped": "recent_capture", "last_completed_at": latest.isoformat()}
            with session or requests.Session() as client:
                rows, started, completed = fetch(client, _token(account))
            cur.execute("""INSERT INTO wb_fbo_intraday_capture
              (account,started_at,completed_at,source_api,source_rows,quantity,
               in_way_to_client,in_way_from_client)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
              (account, started, completed, "stocks-report/wb-warehouses", len(rows),
               sum((r[5] for r in rows), Decimal(0)),
               sum((r[6] for r in rows), Decimal(0)),
               sum((r[7] for r in rows), Decimal(0))))
            capture_id = cur.fetchone()[0]
            execute_values(cur, """INSERT INTO wb_fbo_intraday_item
              (capture_id,nm_id,chrt_id,warehouse_id,warehouse_name,region_name,
               quantity,in_way_to_client,in_way_from_client,raw_payload) VALUES %s""",
              [(capture_id, *row) for row in rows], page_size=500)
    return {"capture_id": capture_id, "account": account, "source_rows": len(rows),
            "completed_at": completed.isoformat()}


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False))
