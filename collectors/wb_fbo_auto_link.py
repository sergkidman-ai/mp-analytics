# поток: fin
"""Infer unique WB acc1 FBO order/stock-out matches from adjacent intraday snapshots."""
import json
from decimal import Decimal

import psycopg2.extras

from core import db

ACCOUNT = "wb_acc1"
SOURCE_WAREHOUSE = "Склад WB РФ"
LOCK_ID = 7348201792

MOVES_SQL = """
WITH pairs AS (
  SELECT c.id current_id,c.completed_at current_at,
         p.id previous_id,p.completed_at previous_at
  FROM wb_fbo_intraday_capture c
  JOIN LATERAL (
    SELECT id,completed_at,source_api FROM wb_fbo_intraday_capture p
    WHERE p.account=c.account AND p.completed_at<c.completed_at
    ORDER BY p.completed_at DESC LIMIT 1
  ) p ON true
  WHERE c.account=%s AND c.completed_at>=now()-interval '7 days'
    AND c.source_api=p.source_api
), previous AS (
  SELECT p.current_id,i.nm_id,sum(i.quantity) quantity,
         sum(i.in_way_to_client) in_way_to_client
  FROM pairs p JOIN wb_fbo_intraday_item i ON i.capture_id=p.previous_id
  GROUP BY p.current_id,i.nm_id
), current AS (
  SELECT p.current_id,i.nm_id,sum(i.quantity) quantity,
         sum(i.in_way_to_client) in_way_to_client
  FROM pairs p JOIN wb_fbo_intraday_item i ON i.capture_id=p.current_id
  GROUP BY p.current_id,i.nm_id
)
SELECT p.previous_id,p.current_id,p.previous_at,p.current_at,
       b.nm_id,b.quantity previous_quantity,c.quantity current_quantity,
       b.in_way_to_client previous_in_way_to_client,
       c.in_way_to_client current_in_way_to_client
FROM pairs p
JOIN previous b ON b.current_id=p.current_id
JOIN current c ON c.current_id=p.current_id AND c.nm_id=b.nm_id
WHERE b.quantity-c.quantity=1
  AND c.in_way_to_client-b.in_way_to_client=1
ORDER BY p.current_at,b.nm_id
"""


def _sku_quantity(order, sku):
    products = order.get("products")
    if not isinstance(products, list):
        return None
    found = [p.get("quantity") for p in products
             if isinstance(p, dict) and str(p.get("sku")) == sku]
    if len(found) != 1:
        return None
    value = found[0]
    try:
        quantity = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return quantity if quantity.is_finite() and quantity > 0 else None


def _contains_sku(order, sku):
    products = order.get("products")
    return isinstance(products, list) and any(
        isinstance(product, dict) and str(product.get("sku")) == sku
        for product in products)


def choose_matches(moves, orders):
    """Only one order containing the SKU may exist inside the observed interval."""
    candidates = []
    for move in moves:
        sku = str(move["nm_id"])
        matching = []
        for order in orders:
            if (move["previous_at"] < order["created_at"] <= move["current_at"]
                    and _contains_sku(order, sku)):
                matching.append((order, _sku_quantity(order, sku)))
        if len(matching) != 1:
            continue
        order, quantity = matching[0]
        if quantity != 1 or order.get("warehouse") != SOURCE_WAREHOUSE:
            continue
        candidates.append((move, order))
    return candidates


def run():
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) locked", (LOCK_ID,))
            if not cur.fetchone()["locked"]:
                return {"busy": True}
            cur.execute(MOVES_SQL, (ACCOUNT,))
            moves = list(cur.fetchall())
            if not moves:
                return {"moves": 0, "candidates": 0, "inserted": 0}
            earliest = min(m["previous_at"] for m in moves)
            latest = max(m["current_at"] for m in moves)
            cur.execute("""SELECT id,posting_number,created_at,warehouse,products
              FROM mp_fbo_orders WHERE platform='wb' AND account=%s
                AND created_at>%s AND created_at<=%s""", (ACCOUNT, earliest, latest))
            candidates = choose_matches(moves, list(cur.fetchall()))
            inserted = 0
            for move, order in candidates:
                evidence = {
                    "kind": "unique_order_intraday_stock_out",
                    "confidence": "inferred_unique",
                    "wb_movement_id": None,
                    "cogs_assigned": False,
                    "source": "stocks-report/wb-warehouses",
                    "previous_observed_at": move["previous_at"].isoformat(),
                    "current_observed_at": move["current_at"].isoformat(),
                    "order_posting_number": order["posting_number"],
                    "order_created_at": order["created_at"].isoformat(),
                    "stock_before": str(move["previous_quantity"]),
                    "stock_after": str(move["current_quantity"]),
                    "in_way_to_client_before": str(move["previous_in_way_to_client"]),
                    "in_way_to_client_after": str(move["current_in_way_to_client"]),
                }
                cur.execute("""INSERT INTO mp_fbo_intraday_link_candidates
                  (account,order_id,sku,source_warehouse,previous_capture_id,
                   current_capture_id,quantity,evidence)
                  VALUES (%s,%s,%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING
                  RETURNING id""", (ACCOUNT, order["id"], str(move["nm_id"]),
                    SOURCE_WAREHOUSE, move["previous_id"], move["current_id"],
                    psycopg2.extras.Json(evidence)))
                inserted += int(cur.fetchone() is not None)
    return {"moves": len(moves), "candidates": len(candidates), "inserted": inserted}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
