# поток: fin
"""Bounded background pilot. A systemd timer wakes it; business due times live in DB."""
import json
from datetime import timedelta
from contextlib import contextmanager

import psycopg2
from core import db
from core.ozon_expense_basis import BasisUnavailable
from customer_guard import service, store
from customer_guard.domain import utcnow
from customer_guard.notifications import recover_interrupted

RUNTIME_LOCK = 7348201702


@contextmanager
def singleton():
    conn=psycopg2.connect(db.DATABASE_URL,connect_timeout=5)
    conn.autocommit=True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)",(RUNTIME_LOCK,))
            locked=cur.fetchone()[0]
        yield locked
    finally:
        conn.close()


def heartbeat(error=None):
    db.execute("UPDATE customer_guard_runtime SET heartbeat_at=%s,last_error=%s WHERE id",
               (utcnow(),error))


def tick():
    with singleton() as locked:
        if not locked:
            return {"busy":True}
        state=db.query("SELECT * FROM customer_guard_runtime WHERE id")[0]
        if not state["enabled"]:
            return {"enabled":False}
        now=utcnow()
        heartbeat()
        recover_interrupted(now)
        result={}
        errors=[]

        def phase(name, operation):
            try:
                result[name]=operation()
                if isinstance(result[name],dict) and result[name].get("notification_errors"):
                    errors.append("notification:"+",".join(result[name]["notification_errors"]))
            except Exception as exc:
                # Request exception strings may contain a token-bearing URL.
                errors.append(name+":"+type(exc).__name__)
            heartbeat(";".join(errors) or None)

        synced=db.query("SELECT started_at FROM customer_guard_sync_state WHERE scope='ozon/oz_acc2/fbs'")
        if not synced or synced[0]["started_at"] <= now-timedelta(minutes=10):
            phase("new_orders",service.sync)
        # Old tracked active orders are point-refreshed, not forgotten outside the list window.
        phase("old_status",lambda:service.refresh(3,older_than=now-timedelta(minutes=10)))
        phase("enqueue",lambda:store.schedule_rechecks(utcnow()))
        phase("work",lambda:service.work(limit=3,budget=100,notify=state["notifications_enabled"]))
        # Basis changes do not require fresh MS reads and do not send old alerts.
        try:
            result["reprice"]=service.reprice()
        except BasisUnavailable:
            errors.append("expense_basis:unavailable")
        except Exception as exc:
            errors.append("reprice:"+type(exc).__name__)
        heartbeat(";".join(errors) or None)
        db.execute("UPDATE customer_guard_runtime SET last_tick_at=%s WHERE id",(utcnow(),))
        result["errors"]=errors
        return result


if __name__=="__main__":
    print(json.dumps(tick(),ensure_ascii=False,default=str))
