# поток: fin
"""Bounded background pilot. A systemd timer wakes it; business due times live in DB."""
import json
from datetime import timedelta
from contextlib import contextmanager

import psycopg2
from core import db
from core.ozon_expense_basis import BasisUnavailable
from customer_guard import service, store
from customer_guard.domain import utcnow, enabled_accounts
from customer_guard.notifications import recover_interrupted

RUNTIME_LOCK = 7348201702
WORK_LIMIT = 10


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
                heartbeat(";".join(errors) or None)
                return True
            except Exception as exc:
                # Request exception strings may contain a token-bearing URL.
                errors.append(name+":"+type(exc).__name__)
                heartbeat(";".join(errors) or None)
                return False

        settings=state.get('settings',{})
        accounts=enabled_accounts(settings)
        def extra_check(name, interval, operation):
            scope='customer_guard/'+name
            checkpoint=db.query('SELECT completed_at FROM customer_guard_sync_state WHERE scope=%s',(scope,))
            if checkpoint and checkpoint[0]['completed_at'] and checkpoint[0]['completed_at']>now-interval:
                return
            if phase(name, operation):
                db.execute("""INSERT INTO customer_guard_sync_state(scope,started_at,completed_at,row_count)
                  VALUES(%s,%s,%s,%s) ON CONFLICT(scope) DO UPDATE SET
                  started_at=excluded.started_at,completed_at=excluded.completed_at,
                  row_count=excluded.row_count,error=NULL""",
                  (scope,now,utcnow(),result[name].get('refreshed',0)))

        from zoneinfo import ZoneInfo
        if 16 <= now.astimezone(ZoneInfo('Europe/Moscow')).hour < 19:
            extra_check('handoff_window',timedelta(minutes=5),
                        lambda:service.observe_handoff_window(accounts,now))
        if db.query("SELECT to_regclass('customer_guard_cancel_review') name")[0]['name']:
            extra_check('cancel_followup',timedelta(hours=2),
                        lambda:service.observe_recent_cancellations(accounts,now))

        synced=db.query("SELECT started_at FROM customer_guard_sync_state WHERE scope='ozon/oz_acc2/fbs'")
        if not synced or synced[0]["started_at"] <= now-timedelta(minutes=10):
            phase("new_orders",service.sync)
        wb_enabled=state.get('settings',{}).get('wb_enabled',False)
        if wb_enabled:
            synced_wb=db.query("SELECT started_at FROM customer_guard_sync_state WHERE scope='wb/wb_acc2/fbs'")
            if not synced_wb or synced_wb[0]['started_at']<=now-timedelta(minutes=10):
                phase('wb_orders',service.sync_wb)
                phase('wb_old_status',lambda:service.refresh_wb(older_than=now-timedelta(minutes=10)))
        if settings.get('digital_enabled',False):
            for acc, platform, operation in (
                ('oz_acc1','ozon',lambda:service.sync(account='oz_acc1')),
                ('wb_acc1','wb',lambda:service.sync_wb(account='wb_acc1')),
                ('ya_acc1','yandex',service.sync_yandex)):
                checkpoint=db.query('SELECT started_at FROM customer_guard_sync_state WHERE scope=%s',
                                    (f'{platform}/{acc}/fbs',))
                if not checkpoint or checkpoint[0]['started_at']<=now-timedelta(minutes=10):
                    phase(acc+'_orders',operation)
                    if platform=='wb':
                        phase(acc+'_old_status',lambda:service.refresh_wb(account='wb_acc1',older_than=now-timedelta(minutes=10)))
            phase('oz_acc1_old_status',lambda:service.refresh(3,account='oz_acc1',older_than=now-timedelta(minutes=10)))
            phase('ya_acc1_old_status',lambda:service.refresh_yandex(3,older_than=now-timedelta(minutes=10)))
        # Old tracked active orders are point-refreshed, not forgotten outside the list window.
        phase("old_status",lambda:service.refresh(3,older_than=now-timedelta(minutes=10)))
        phase("enqueue",lambda:store.schedule_rechecks(utcnow(),int(settings.get('repeat_minutes',120))))
        phase("reuse_actual",lambda:store.reuse_confirmed_actual(utcnow()))
        phase("work",lambda:service.work(limit=WORK_LIMIT,budget=100,notify=state["notifications_enabled"],
                                         accounts=enabled_accounts(settings)))
        # Basis changes do not require fresh MS reads and do not send old alerts.
        try:
            result["reprice"]=service.reprice()
        except BasisUnavailable:
            errors.append("expense_basis:unavailable")
        except Exception as exc:
            errors.append("reprice:"+type(exc).__name__)
        if wb_enabled:
            phase('wb_reprice',lambda:service.reprice(account='wb_acc2'))
        if settings.get('digital_enabled',False):
            for acc in ('oz_acc1','wb_acc1','ya_acc1'):
                phase(acc+'_reprice',lambda acc=acc:service.reprice(account=acc))
        heartbeat(";".join(errors) or None)
        db.execute("UPDATE customer_guard_runtime SET last_tick_at=%s WHERE id",(utcnow(),))
        result["errors"]=errors
        return result


if __name__=="__main__":
    print(json.dumps(tick(),ensure_ascii=False,default=str))
