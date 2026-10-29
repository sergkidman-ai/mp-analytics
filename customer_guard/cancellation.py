# поток: fin
"""Local, evidence-backed cancellation review. Never changes MoySklad orders."""
from psycopg2.extras import Json

UNKNOWN = 'Отменён — путь товара выясняется'
HANDED_OFF = 'Отменён — передан МП, маршрут возврата выясняется'
CATEGORIES = (
    'Отменён — товар не покидал наш склад', UNKNOWN, HANDED_OFF,
    'Невыкуп → наш склад', 'Невыкуп → склад МП',
    'Возврат после вручения → наш склад', 'Возврат после вручения → склад МП',
    'Возврат принят нами', 'Возврат принят складом МП',
)

OZON_HANDOFF = {
    'posting_acceptance_in_progress', 'posting_driver_pick_up',
    'posting_on_way_to_city', 'posting_in_pickup_point',
    'posting_in_courier_service', 'posting_transferred_to_courier_service',
    'posting_conditionally_delivered', 'posting_received',
}
WB_HANDOFF = {
    'sorted', 'accepted_by_carrier', 'sent_to_carrier',
    'ready_for_pickup', 'postponed_delivery', 'sold',
}
YANDEX_HANDOFF = {
    'DELIVERY_SERVICE_RECEIVED', 'PICKUP_SERVICE_RECEIVED',
    'FIRST_MILE_DELIVERY_SERVICE_RECEIVED', 'COURIER_RECEIVED',
    'READY_FOR_LAST_MILE', 'LAST_MILE_STARTED',
    'LAST_MILE_COURIER_SEARCH', 'DELIVERY_SERVICE_DELIVERED',
    'PICKUP_USER_RECEIVED', 'USER_RECEIVED',
}


def handoff(platform, status, substatus):
    if platform == 'ozon':
        return substatus in OZON_HANDOFF or status == 'delivered'
    if platform == 'wb':
        # supplierStatus=complete and wbStatus=waiting is *not* receipt by WB.
        return substatus.split(':')[-1] in WB_HANDOFF or status == 'delivered'
    if platform == 'yandex':
        return substatus in YANDEX_HANDOFF or status == 'delivered'
    return False


def classify(platform, observations):
    """Observations are (fetched_at, status, substatus); timestamps are polls."""
    canceled = next((t for t, status, _ in observations if status == 'cancelled'), None)
    if canceled is None:
        return None
    proof = next(((t, sub) for t, status, sub in observations
                  if handoff(platform, status, sub)), None)
    received = next((t for t, status, _ in observations if status == 'delivered'), None)
    category = HANDED_OFF if proof else UNKNOWN
    evidence = {'source': platform, 'handoff_status': proof[1] if proof else None,
                'handoff_first_seen_at': proof[0].isoformat() if proof else None,
                'buyer_received_first_seen_at': received.isoformat() if received else None,
                'basis': 'marketplace_snapshot_history'}
    return category, canceled, proof[0] if proof else None, received, evidence


def review(cur, order_id, platform, now):
    cur.execute("""SELECT fetched_at,payload->>'status' status,
        coalesce(payload->>'substatus','') substatus
      FROM customer_guard_snapshots WHERE order_id=%s AND source=%s
      ORDER BY fetched_at,id""", (order_id, platform))
    finding = classify(platform, [(r['fetched_at'],r['status'],r['substatus'])
                                  for r in cur.fetchall()])
    if finding is None:
        return None
    category, canceled, handoff_at, received_at, evidence = finding
    cur.execute("""SELECT category FROM customer_guard_cancel_review
      WHERE order_id=%s FOR UPDATE""", (order_id,))
    previous = cur.fetchone()
    if previous and previous['category'] not in (UNKNOWN, HANDED_OFF):
        return previous['category']  # Later receipt or human review has precedence.
    cur.execute("""INSERT INTO customer_guard_cancel_review
      (order_id,category,first_cancelled_at,first_handoff_at,buyer_received_at,evidence,reviewed_at)
      VALUES (%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT(order_id) DO UPDATE SET
        category=excluded.category,first_cancelled_at=excluded.first_cancelled_at,
        first_handoff_at=excluded.first_handoff_at,
        buyer_received_at=excluded.buyer_received_at,evidence=excluded.evidence,
        reviewed_at=excluded.reviewed_at""",
      (order_id, category, canceled, handoff_at, received_at, Json(evidence), now))
    if previous is None or previous['category'] != category:
        cur.execute("""INSERT INTO customer_guard_cancel_review_history
          (order_id,category,observed_at,evidence) VALUES (%s,%s,%s,%s)""",
          (order_id,category,now,Json(evidence)))
    return category
