# поток: fin
"""Infer unique WB order/stock-out candidates for both accounts, including update lag."""
import json
from collections import Counter
from decimal import Decimal
from datetime import timedelta

import psycopg2.extras

from core import db

ACCOUNT = "wb_acc1"
ACCOUNTS = ("wb_acc1", "wb_acc2")
SOURCE_WAREHOUSE = "Склад WB РФ"
WAREHOUSE_ALIASES = {SOURCE_WAREHOUSE, "Склад WB"}
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
            if (move.get("order_window_start", move["previous_at"]) < order["created_at"] <= move["current_at"]
                    and _contains_sku(order, sku)):
                matching.append((order, _sku_quantity(order, sku)))
        if len(matching) != 1:
            continue
        order, quantity = matching[0]
        if (quantity != 1 or order.get("warehouse") not in WAREHOUSE_ALIASES
                or order.get('mp_status') in ('cancelled', 'returned', 'returned_defective')):
            continue
        candidates.append((move, order))
    counts = Counter((o['id'],str(m['nm_id'])) for m,o in candidates)
    return [(m,o) for m,o in candidates if counts[(o['id'],str(m['nm_id']))] == 1]


def delayed_moves(observations):
    """One stock-out followed by one transit increase, with no intervening changes.

    WB updates these counters asynchronously. The 24h order window is explicit
    candidate evidence, never a movement identity or permission to assign COGS.
    Missing SKU observations, source changes and gaps over 12h break the chain.
    """
    grouped = {}
    for row in observations:
        grouped.setdefault(row['nm_id'], []).append(row)
    moves = []
    for nm, rows in grouped.items():
        rows = sorted(rows, key=lambda r: r['completed_at'])
        for index in range(1, len(rows)):
            before, drop = rows[index-1:index+1]
            if (drop['seq'] != before['seq']+1 or drop['source_api'] != before['source_api']
                    or drop['completed_at']-before['completed_at'] > timedelta(hours=12)
                    or before['quantity']-drop['quantity'] != 1
                    or before['transit'] != drop['transit']):
                continue
            last = drop
            for later in rows[index+1:]:
                if (later['seq'] != last['seq']+1 or later['source_api'] != before['source_api']
                        or later['completed_at']-drop['completed_at'] > timedelta(hours=4)
                        or later['quantity'] != drop['quantity']):
                    break
                increase = later['transit']-drop['transit']
                if increase == 1:
                    moves.append(dict(nm_id=nm,previous_id=before['id'],current_id=later['id'],
                        previous_at=before['completed_at'],current_at=later['completed_at'],
                        previous_quantity=before['quantity'],current_quantity=later['quantity'],
                        previous_in_way_to_client=before['transit'],current_in_way_to_client=later['transit'],
                        order_window_start=later['completed_at']-timedelta(hours=24),
                        stock_drop_observed_at=drop['completed_at'], delayed=True))
                    break
                if increase != 0:
                    break
                last = later
    return moves


def run(account=ACCOUNT):
    if account not in ACCOUNTS:
        raise ValueError('unknown_wb_inventory_account')
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) locked", (LOCK_ID + 100 * ACCOUNTS.index(account),))
            if not cur.fetchone()["locked"]:
                return {"busy": True}
            cur.execute(MOVES_SQL, (account,))
            moves = list(cur.fetchall())
            cur.execute('''WITH caps AS (
              SELECT *,row_number() over(order by completed_at) seq
              FROM wb_fbo_intraday_capture WHERE account=%s AND completed_at>=now()-interval '7 days')
              SELECT c.id,c.completed_at,c.source_api,c.seq,i.nm_id,
                sum(i.quantity) quantity,sum(i.in_way_to_client) transit
              FROM caps c JOIN wb_fbo_intraday_item i ON i.capture_id=c.id
              GROUP BY c.id,c.completed_at,c.source_api,c.seq,i.nm_id''', (account,))
            moves.extend(delayed_moves(list(cur.fetchall())))
            if not moves:
                return {"moves": 0, "candidates": 0, "inserted": 0}
            earliest = min(m.get('order_window_start', m['previous_at']) for m in moves)
            latest = max(m["current_at"] for m in moves)
            cur.execute("""SELECT id,posting_number,created_at,warehouse,products,mp_status
              FROM mp_fbo_orders WHERE platform='wb' AND account=%s
                AND created_at>%s AND created_at<=%s""", (account, earliest, latest))
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
                if move.get('delayed'):
                    evidence.update(kind='unique_order_delayed_stock_out', confidence='inferred_delayed',
                        order_window_start=move['order_window_start'].isoformat(),
                        stock_drop_observed_at=move['stock_drop_observed_at'].isoformat(),
                        max_order_lag_hours=24, max_transit_lag_hours=4)
                cur.execute("""INSERT INTO mp_fbo_intraday_link_candidates
                  (account,order_id,sku,source_warehouse,previous_capture_id,
                   current_capture_id,quantity,evidence)
                  VALUES (%s,%s,%s,%s,%s,%s,1,%s) ON CONFLICT DO NOTHING
                  RETURNING id""", (account, order["id"], str(move["nm_id"]),
                    SOURCE_WAREHOUSE, move["previous_id"], move["current_id"],
                    psycopg2.extras.Json(evidence)))
                inserted += int(cur.fetchone() is not None)
    return {"moves": len(moves), "candidates": len(candidates), "inserted": inserted}


if __name__ == "__main__":
    print(json.dumps({a:run(a) for a in ACCOUNTS}, ensure_ascii=False))
