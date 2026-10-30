# поток: fin
"""Track unambiguous WB returns from negative order to transit and receipt evidence.

Only reads existing Pult/WB/MS tables. Never assigns COGS or changes MS documents.
"""
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from psycopg2.extras import Json, RealDictCursor

from core import db

ACCOUNTS = ("wb_acc1", "wb_acc2")
SELLER_STORES = ("Звездный", "Дисквер", "Брак")
LOCK_ID = 7348201793


def _one_unit_skus(products):
    if not isinstance(products, list):
        return []
    counts = defaultdict(Decimal)
    for product in products:
        if not isinstance(product, dict):
            continue
        sku = str(product.get("sku", ""))
        if not (sku.isdigit() and len(sku) <= 18):
            continue
        try:
            qty = Decimal(str(product.get("quantity")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if qty.is_finite() and qty > 0:
            counts[sku] += qty
    return [sku for sku, qty in counts.items() if qty == 1]


def _events(cur):
    cur.execute("""SELECT id,account,posting_number,status_at event_at,products
      FROM mp_fbo_orders WHERE platform='wb' AND account=ANY(%s)
        AND mp_status IN ('cancelled','returned')
        AND status_at>=now()-interval '30 days' AND status_at<=now()
        AND status_at>=created_at""", (list(ACCOUNTS),))
    rows = [dict(row, order_kind="fbo") for row in cur.fetchall()]
    cur.execute("""SELECT o.id,o.account,o.posting_number,
      r.first_cancelled_at event_at,o.mp_payload->'products' products
      FROM customer_guard_cancel_review r
      JOIN customer_guard_orders o ON o.id=r.order_id
      WHERE o.platform='wb' AND o.account=ANY(%s)
        AND o.mp_status='cancelled' AND r.first_handoff_at IS NOT NULL
        AND r.first_handoff_at<=r.first_cancelled_at
        AND r.first_cancelled_at>=now()-interval '30 days'
        AND r.first_cancelled_at<=now()""", (list(ACCOUNTS),))
    rows.extend(dict(row, order_kind="fbs") for row in cur.fetchall())
    events = []
    for row in rows:
        for sku in _one_unit_skus(row["products"]):
            events.append({**row, "sku": sku})
    return events


def _snapshots(cur, account, skus):
    """Daily snapshots until the first intraday one; thereafter one consistent API."""
    cur.execute("""SELECT min(completed_at) first_at FROM wb_fbo_intraday_capture
      WHERE account=%s""", (account,))
    first_intraday = cur.fetchone()["first_at"]
    cur.execute("""SELECT captured_at,recorded_at,source_rows FROM mp_stock_capture
      WHERE platform='wb' AND account=%s AND captured_at>=current_date-30
        AND captured_at>='2026-10-26' AND source_rows>0
      ORDER BY captured_at""", (account,))
    daily = [r for r in cur.fetchall()
             if first_intraday is None or r["recorded_at"] < first_intraday]
    cur.execute("""SELECT captured_at,sku,
      sum(CASE WHEN warehouse='(в пути)' THEN coalesce(in_way_from_client,0)
               ELSE 0 END) transit,
      sum(CASE WHEN warehouse='Склад WB РФ' THEN raw_available ELSE 0 END) stock
      FROM mp_stock_history WHERE platform='wb' AND account=%s
        AND captured_at>=current_date-30 AND sku=ANY(%s)
        AND warehouse IN ('(в пути)','Склад WB РФ')
      GROUP BY captured_at,sku""", (account, skus))
    daily_values = {(r["captured_at"], r["sku"]): r for r in cur.fetchall()}
    cur.execute("""SELECT id,completed_at,source_rows FROM wb_fbo_intraday_capture
      WHERE account=%s AND completed_at>=now()-interval '30 days'
      ORDER BY completed_at""", (account,))
    intraday = list(cur.fetchall())
    cur.execute("""SELECT i.capture_id,i.nm_id::text sku,sum(i.in_way_from_client) transit,
      sum(i.quantity) stock FROM wb_fbo_intraday_item i
      JOIN wb_fbo_intraday_capture c ON c.id=i.capture_id
      WHERE c.account=%s AND c.completed_at>=now()-interval '30 days'
        AND i.nm_id=ANY(%s)
      GROUP BY i.capture_id,i.nm_id""", (account, [int(s) for s in skus]))
    intraday_values = {(r["capture_id"], r["sku"]): r for r in cur.fetchall()}
    snapshots = defaultdict(lambda: defaultdict(list))
    for capture in daily:
        for sku in skus:
            row = daily_values.get((capture["captured_at"], sku), {})
            snapshots[account][sku].append(dict(source_kind="daily", at=capture["recorded_at"],
                capture=str(capture["captured_at"]), transit=row.get("transit") or Decimal(0),
                stock=row.get("stock") or Decimal(0)))
    for capture in intraday:
        for sku in skus:
            row = intraday_values.get((capture["id"], sku), {})
            snapshots[account][sku].append(dict(source_kind="intraday", at=capture["completed_at"],
                capture=str(capture["id"]), transit=row.get("transit") or Decimal(0),
                stock=row.get("stock") or Decimal(0)))
    return snapshots[account]


def adjacent_pairs(observations):
    ordered = sorted(observations, key=lambda x: x["at"])
    return [(a, b) for a, b in zip(ordered, ordered[1:])
            if a["source_kind"] == b["source_kind"] and a["at"] < b["at"]]


def choose_transit_candidates(events, snapshots):
    selected = []
    by_key = defaultdict(list)
    for event in events:
        by_key[event["account"], event["sku"]].append(event)
    for (account, sku), related in by_key.items():
        for previous, current in adjacent_pairs(snapshots.get(account, {}).get(sku, [])):
            if current["transit"] - previous["transit"] != 1:
                continue
            matching = [e for e in related if previous["at"] < e["event_at"] <= current["at"]]
            if len(matching) == 1:
                selected.append((matching[0], previous, current))
    return selected


def choose_wb_receipt(candidate, observations, events, active_candidates):
    """The candidate must be the sole transit unit throughout the final interval."""
    if candidate["transit_before"] != 0 or candidate["transit_after"] != 1:
        return None
    after_origin = [(a, b) for a, b in adjacent_pairs(observations)
                    if a["at"] >= candidate["current_observed_at"]
                    and a["source_kind"] == candidate["source_kind"]]
    for previous, current in after_origin:
        competing = sum(c["account"] == candidate["account"] and c["sku"] == candidate["sku"]
            and c["stage"] == "in_transit_candidate"
            and c["current_observed_at"] <= current["at"] for c in active_candidates)
        if competing != 1:
            break
        if previous["transit"] == 1 and current["transit"] == 1:
            continue
        if (previous["transit"] == 1 and current["transit"] == 0
                and current["stock"] - previous["stock"] == 1
                and not any(e["account"] == candidate["account"] and e["sku"] == candidate["sku"]
                            and candidate["current_observed_at"] < e["event_at"] <= current["at"]
                            for e in events)):
            return previous, current
        break
    return None


def _insert_candidate(cur, event, previous, current):
    evidence = {"kind": "unique_negative_order_and_return_transit_rise",
        "confidence": "inferred_candidate", "cogs_assigned": False,
        "wb_movement_id": None, "negative_status_at": event["event_at"].isoformat(),
        "source_kind": current["source_kind"], "previous_capture": previous["capture"],
        "current_capture": current["capture"], "stock_before": str(previous["stock"]),
        "stock_after": str(current["stock"])}
    cur.execute("""INSERT INTO wb_return_journey_candidate
      (account,order_kind,order_id,posting_number,sku,source_kind,
       previous_observed_at,current_observed_at,transit_before,transit_after,evidence)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT DO NOTHING RETURNING id""",
      (event["account"],event["order_kind"],event["id"],event["posting_number"],event["sku"],
       current["source_kind"],previous["at"],current["at"],previous["transit"],
       current["transit"],Json(evidence)))
    row = cur.fetchone()
    if not row:
        return False
    cur.execute("""INSERT INTO wb_return_journey_event(candidate_id,stage,observed_at,evidence)
      VALUES (%s,'in_transit_candidate',%s,%s)""", (row["id"],current["at"],Json(evidence)))
    return True


def _seller_receipts(cur, candidates):
    numbers = [c["posting_number"] for c in candidates if c["order_kind"] == "fbs"]
    if not numbers:
        return {}
    cur.execute("""SELECT demand_name,return_id,moment,store,ret_qty
      FROM ms_return_cogs WHERE demand_name=ANY(%s) AND store=ANY(%s)
        AND ret_qty>=1 ORDER BY moment""", (numbers,list(SELLER_STORES)))
    receipts = defaultdict(list)
    for row in cur.fetchall():
        receipts[row["demand_name"]].append(row)
    return receipts


def _confirm(cur, candidate, stage, observed_at, evidence):
    cur.execute("""UPDATE wb_return_journey_candidate SET stage=%s,
      destination_observed_at=%s,evidence=evidence || %s,updated_at=now()
      WHERE id=%s AND stage='in_transit_candidate' RETURNING id""",
      (stage,observed_at,Json(evidence),candidate["id"]))
    if not cur.fetchone():
        return False
    cur.execute("""INSERT INTO wb_return_journey_event(candidate_id,stage,observed_at,evidence)
      VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
      (candidate["id"],stage,observed_at,Json(evidence)))
    return True


def run():
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) locked", (LOCK_ID,))
            if not cur.fetchone()["locked"]:
                return {"busy": True}
            events = _events(cur)
            cur.execute("""SELECT * FROM wb_return_journey_candidate
              WHERE stage='in_transit_candidate'""")
            active = list(cur.fetchall())
            skus = defaultdict(set)
            for row in events + active:
                skus[row["account"]].add(row["sku"])
            snapshots = {account: _snapshots(cur,account,sorted(skus[account]))
                         for account in ACCOUNTS if skus[account]}
            selected = choose_transit_candidates(events,snapshots)
            inserted = sum(_insert_candidate(cur,*item) for item in selected)
            cur.execute("""SELECT * FROM wb_return_journey_candidate
              WHERE stage='in_transit_candidate'""")
            active = list(cur.fetchall())
            receipts = _seller_receipts(cur,active)
            received_seller = received_wb = 0
            for candidate in active:
                seller = next((r for r in receipts.get(candidate["posting_number"],[])
                               if r["moment"] >= candidate["current_observed_at"]),None)
                if seller:
                    evidence = {"destination":"seller","receipt_source":"ms_return_cogs",
                        "ms_return_id":seller["return_id"],"store":seller["store"],
                        "receipt_at":seller["moment"].isoformat(),"cogs_assigned":False}
                    received_seller += _confirm(cur,candidate,"received_seller",seller["moment"],evidence)
                    continue
                pair = choose_wb_receipt(candidate,
                    snapshots.get(candidate["account"],{}).get(candidate["sku"],[]),
                    events,active)
                if not pair:
                    continue
                previous,current = pair
                evidence = {"destination":"wb","receipt_source":current["source_kind"],
                    "receipt_previous_capture":previous["capture"],
                    "receipt_current_capture":current["capture"],
                    "transit_before":str(previous["transit"]),
                    "transit_after":str(current["transit"]),
                    "stock_before":str(previous["stock"]),
                    "stock_after":str(current["stock"]),
                    "wb_movement_id":None,"cogs_assigned":False}
                received_wb += _confirm(cur,candidate,"received_wb",current["at"],evidence)
    return {"negative_events":len(events),"transit_candidates":len(selected),
            "inserted":inserted,"received_wb":received_wb,"received_seller":received_seller}


if __name__ == "__main__":
    print(json.dumps(run(),ensure_ascii=False))
