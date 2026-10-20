# поток: fin
"""Transactional observations, leased first checks and durable incidents."""
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from psycopg2.extras import Json, RealDictCursor
from core import db
from customer_guard.domain import ACTIVE, TERMINAL, digest, initial_due, posting_snapshot, posting_hash, sale


@contextmanager
def transaction():
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout='10s'")
            yield cur


def observe(cur, post, observed_at, received_at, api_version="fbs/list/v4"):
    post = posting_snapshot(post)
    sale(post)  # Reject invalid inputs before acknowledging a complete sync.
    fingerprint = posting_hash(post)
    cur.execute("""SELECT * FROM customer_guard_orders
      WHERE platform='ozon' AND account='oz_acc2' AND posting_number=%s FOR UPDATE""",
                (post["posting_number"],))
    old = cur.fetchone()
    if old and observed_at < old["observed_at"]:
        return dict(old)
    changed = old is None or old["mp_hash"] != fingerprint
    if old:
        generation = old["generation"] + int(changed)
        cur.execute("""UPDATE customer_guard_orders SET mp_status=%s,mp_hash=%s,
          mp_payload=%s,observed_at=%s,generation=%s WHERE id=%s RETURNING *""",
                    (post["status"], fingerprint, Json(post), observed_at, generation, old["id"]))
    else:
        cur.execute("""INSERT INTO customer_guard_orders
          (posting_number,parent_number,first_seen_at,observed_at,ms_due_at,mp_status,mp_hash,mp_payload)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
          (post["posting_number"], str(post.get("order_number") or ""), received_at, observed_at,
           initial_due(received_at), post["status"], fingerprint, Json(post)))
    order = dict(cur.fetchone())
    cur.execute("""INSERT INTO customer_guard_snapshots
       (order_id,source,api_version,fetched_at,hash,payload) VALUES(%s,'ozon',%s,%s,%s,%s)""",
       (order["id"], api_version, observed_at, fingerprint, Json(post)))
    if changed and post["status"] in ACTIVE:
        due = max(order["ms_due_at"], received_at)
        cur.execute("""INSERT INTO customer_guard_jobs(order_id,generation,due_at,kind)
          VALUES(%s,%s,%s,%s) ON CONFLICT(order_id) DO UPDATE SET
          generation=excluded.generation,due_at=excluded.due_at,kind=excluded.kind,
          state='pending',attempts=0,lease_token=NULL,lease_until=NULL,error=NULL""",
          (order["id"], order["generation"], due, "recalculate" if old else "initial_ms_check"))
    elif post["status"] not in ACTIVE:
        cur.execute("UPDATE customer_guard_jobs SET state='done',lease_token=NULL,lease_until=NULL WHERE order_id=%s",
                    (order["id"],))
        if post["status"] in TERMINAL:
            cur.execute("""UPDATE customer_guard_incidents SET resolved_at=%s,
              delivery=CASE WHEN delivery='pending' THEN 'suppressed' ELSE delivery END
              WHERE order_id=%s AND resolved_at IS NULL""", (received_at, order["id"]))
    return order


def ingest(posts, started_at, received_at, window_from, window_to, *, mirror_raw=False):
    # Entire window, including raw, checkpoint and jobs, commits together.
    with transaction() as cur:
        for post in posts:
            observe(cur, post, started_at, received_at)
            # V4 Money must not reach legacy financial consumers before wb4c deploys.
            # Pilot snapshots contain the exact unmodified calculation input fields.
            if not mirror_raw:
                continue
            cur.execute("""INSERT INTO raw_ozon_posting
              (account,posting_number,scheme,status,in_process_at,period_from,period_to,payload,loaded_at)
              VALUES('oz_acc2',%s,'fbs',%s,%s,%s,%s,%s,%s)
              ON CONFLICT(account,posting_number) DO UPDATE SET
              scheme=excluded.scheme,status=excluded.status,in_process_at=excluded.in_process_at,
              period_from=excluded.period_from,period_to=excluded.period_to,
              payload=excluded.payload,loaded_at=excluded.loaded_at
              WHERE raw_ozon_posting.loaded_at<=excluded.loaded_at""",
              (post["posting_number"], post["status"], post.get("in_process_at") or post.get("created_at"),
               window_from.date(), window_to.date(), Json(post), started_at))
        cur.execute("""INSERT INTO customer_guard_sync_state
          (scope,started_at,completed_at,window_from,window_to,row_count,error)
          VALUES('ozon/oz_acc2/fbs',%s,%s,%s,%s,%s,NULL)
          ON CONFLICT(scope) DO UPDATE SET started_at=excluded.started_at,
          completed_at=excluded.completed_at,window_from=excluded.window_from,
          window_to=excluded.window_to,row_count=excluded.row_count,error=NULL""",
          (started_at, received_at, window_from, window_to, len(posts)))


def claim(now):
    token = str(uuid4())
    with transaction() as cur:
        cur.execute("""SELECT j.order_id FROM customer_guard_jobs j
          WHERE (j.state IN ('pending','retry') AND j.due_at<=%s)
             OR (j.state='running' AND j.lease_until<%s)
          ORDER BY j.due_at FOR UPDATE SKIP LOCKED LIMIT 1""", (now, now))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""UPDATE customer_guard_jobs SET state='running',attempts=attempts+1,
          lease_token=%s,lease_until=%s WHERE order_id=%s RETURNING *""",
          (token, now + timedelta(minutes=10), row["order_id"]))
        job = dict(cur.fetchone())
        cur.execute("SELECT * FROM customer_guard_orders WHERE id=%s", (row["order_id"],))
        job["order"] = dict(cur.fetchone())
        return job


def refresh(job, post, now):
    with transaction() as cur:
        order = observe(cur, post, now, now, "fbs/get/v3")
    # Changed inputs invalidate the old lease. A new pending job owns the revision.
    return order["generation"] == job["generation"] and order["mp_status"] in ACTIVE


def finish(job, result, now, document=None, evidence=None):
    with transaction() as cur:
        cur.execute("""SELECT o.generation,j.lease_token,j.lease_until
          FROM customer_guard_orders o JOIN customer_guard_jobs j ON j.order_id=o.id
          WHERE o.id=%s FOR UPDATE OF o,j""", (job["order_id"],))
        row = cur.fetchone()
        if (not row or row["generation"] != job["generation"]
                or str(row["lease_token"]) != str(job["lease_token"]) or row["lease_until"] <= now):
            return False
        payload = dict(document=document, evidence=evidence or [])
        cur.execute("""INSERT INTO customer_guard_snapshots(order_id,source,api_version,fetched_at,hash,payload)
          VALUES(%s,'ms','remap/1.2',%s,%s,%s)""", (job["order_id"], now, digest(payload), Json(payload)))
        cur.execute("""INSERT INTO customer_guard_checks(order_id,generation,calculated_at,result,input_hash)
          VALUES(%s,%s,%s,%s,%s) RETURNING id""",
          (job["order_id"], job["generation"], now, Json(result), digest([job["order"]["mp_hash"], payload])))
        cid = cur.fetchone()["id"]
        cur.execute("""UPDATE customer_guard_orders SET latest_check_id=%s,ms_checked_at=%s,
          ms_id=%s,ms_status=%s,ms_store=%s WHERE id=%s""",
          (cid, now, document.get("id") if document else None,
           document.get("state", {}).get("name") if document else None,
           document.get("store", {}).get("name") if document else None, job["order_id"]))
        if document:
            cur.execute("""INSERT INTO customer_guard_ms_links(order_id,entity_type,entity_id,checked_at,version)
              VALUES(%s,'customerorder',%s,%s,%s) ON CONFLICT(order_id,entity_type,entity_id)
              DO UPDATE SET checked_at=excluded.checked_at,version=excluded.version""",
              (job["order_id"], document["id"], now, document.get("updated")))
        for source in result.get("cost_sources", []):
            cur.execute("""INSERT INTO customer_guard_cost_sources(order_id,check_id,source,observed_at)
              VALUES(%s,%s,%s,%s)""", (job["order_id"], cid, Json(source), now))
            if result.get("cost_kind") == "actual":
                cur.execute("""INSERT INTO customer_guard_ms_links(order_id,entity_type,entity_id,checked_at,version)
                  VALUES(%s,'demand',%s,%s,%s) ON CONFLICT(order_id,entity_type,entity_id)
                  DO UPDATE SET checked_at=excluded.checked_at,version=excluded.version""",
                  (job["order_id"], source["entity_id"], now, source.get("version")))
        kind = result.get("signal")
        if kind == "missing_ms":
            cur.execute("""INSERT INTO customer_guard_incidents
              (order_id,kind,dedup_key,opened_at,check_id) VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(dedup_key) DO NOTHING""",
              (job["order_id"], kind, f"oz_acc2:{job['order']['posting_number']}:missing_ms", now, cid))
        elif document:
            cur.execute("""UPDATE customer_guard_incidents SET resolved_at=%s,
              delivery=CASE WHEN delivery='pending' THEN 'suppressed' ELSE delivery END
              WHERE order_id=%s AND resolved_at IS NULL""", (now, job["order_id"]))
        cur.execute("""UPDATE customer_guard_jobs SET state='done',lease_token=NULL,
          lease_until=NULL,error=NULL WHERE order_id=%s""", (job["order_id"],))
        return True


def fail(job, code, now, delay=600):
    with transaction() as cur:
        cur.execute("""UPDATE customer_guard_jobs SET state=CASE WHEN attempts>=5 THEN 'error' ELSE 'retry' END,
          error=%s,due_at=%s,lease_token=NULL,lease_until=NULL
          WHERE order_id=%s AND lease_token=%s AND generation=%s""",
          (code, now + timedelta(seconds=max(600, delay)), job["order_id"], job["lease_token"], job["generation"]))
