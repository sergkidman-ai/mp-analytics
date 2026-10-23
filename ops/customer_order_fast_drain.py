# поток: fin
"""Temporary fast drain of the current customer-order MS backlog.

Only this one-shot runner uses a shorter MS spacing. It keeps the shared worker
lock, five-order batch, 100-GET budget and 240-second client deadline. Normal
systemd operation is restored in finally, including on SIGTERM.
"""
import json
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from core import db
from customer_guard import runtime, service
from customer_guard.domain import ACTIVE, enabled_accounts
from customer_guard.sources import CustomerMS

TIMER = "customer-order-guard.timer"
SERVICE = "customer-order-guard.service"
FAST_INTERVAL = 0.25
SYNC_INTERVAL = timedelta(minutes=10)
MAX_RUNTIME = timedelta(hours=3)


class FastCustomerMS(CustomerMS):
    """Process-local pacing override; the deployed worker retains its normal rate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interval = FAST_INTERVAL


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def run_systemctl(*args):
    return subprocess.run(["systemctl", *args], check=True, timeout=660)


def active_state():
    return subprocess.check_output(
        ["systemctl", "show", SERVICE, "-p", "ActiveState", "--value"], text=True
    ).strip()


def due_ids(now):
    rows = db.query("""SELECT o.id FROM customer_guard_jobs j
      JOIN customer_guard_orders o ON o.id=j.order_id
      WHERE ((j.state IN ('pending','retry') AND j.due_at<=%s)
         OR (j.state='running' AND j.lease_until<%s))
      AND o.mp_status=ANY(%s)""", (now, now, list(ACTIVE)))
    return [row["id"] for row in rows]


def remaining(ids, started):
    return db.query("""SELECT count(*) AS n FROM customer_guard_orders o
      WHERE o.id=ANY(%s) AND o.mp_status=ANY(%s)
      AND (o.ms_checked_at IS NULL OR o.ms_checked_at<%s)""",
      (ids, list(ACTIVE), started))[0]["n"]


def due_count():
    return db.query("""SELECT count(*) AS n FROM customer_guard_jobs j
      JOIN customer_guard_orders o ON o.id=j.order_id
      WHERE o.mp_status=ANY(%s) AND
      ((j.state IN ('pending','retry') AND j.due_at<=now())
       OR (j.state='running' AND j.lease_until<now()))""",
      (list(ACTIVE),))[0]["n"]


def metric(name):
    rows = db.query("SELECT value FROM ms_shipment_metrics WHERE name=%s", (name,))
    return rows[0]["value"] if rows else 0


def on_stop(signum, frame):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, on_stop)
    started = datetime.now(timezone.utc)
    ids = due_ids(started)
    original = len(ids)
    initial_requests = metric("ms_requests")
    initial_429 = metric("ms_429")
    emit(event="start", due_cohort=original, started_at=started,
         spacing_seconds=FAST_INTERVAL)
    if not ids:
        return
    run_systemctl("stop", TIMER)
    try:
        while active_state() not in ("inactive", "failed"):
            time.sleep(2)
        if active_state() == "failed":
            raise RuntimeError("guard_unit_failed")
        service.CustomerMS = FastCustomerMS
        previous_tick = db.query("SELECT last_tick_at FROM customer_guard_runtime WHERE id")[0]["last_tick_at"]
        next_sync = max(started, previous_tick+SYNC_INTERVAL) if previous_tick else started
        stalled = 0
        last_remaining = original
        for batch in range(1, 2001):
            now = datetime.now(timezone.utc)
            if now-started >= MAX_RUNTIME:
                emit(event="time_limit", batch=batch, remaining=last_remaining)
                break
            if now >= next_sync:
                result = runtime.tick()
                emit(event="sync", batch=batch, errors=result.get("errors", []),
                     normal_work=result.get("work"))
                next_sync = datetime.now(timezone.utc)+SYNC_INTERVAL
                if result.get("errors"):
                    raise RuntimeError("runtime_sync_error")
            state = db.query("SELECT enabled,notifications_enabled,settings FROM customer_guard_runtime WHERE id")[0]
            if not state["enabled"]:
                raise RuntimeError("runtime_disabled")
            client = FastCustomerMS(budget=100)
            result = service.work(limit=5, budget=100, client=client,
                                  notify=state["notifications_enabled"],
                                  accounts=enabled_accounts(state["settings"]))
            current = remaining(ids, started)
            if batch == 1 or batch % 5 == 0 or current == 0:
                emit(event="batch", batch=batch, remaining=current,
                     cleared=original-current, live_due=due_count(),
                     completed=result["completed"], ms_requests=result["ms_requests"],
                     total_ms_requests=metric("ms_requests")-initial_requests)
            if current == 0:
                emit(event="complete", batches=batch, cohort=original)
                return
            if metric("ms_429") > initial_429:
                raise RuntimeError("ms_429")
            stalled = stalled+1 if current >= last_remaining else 0
            if result["completed"] == 0 or stalled >= 3:
                emit(event="cooldown", batch=batch, seconds=180,
                     reason="no_progress")
                time.sleep(180)
            if stalled >= 9:
                raise RuntimeError("persistent_no_progress")
            last_remaining = current
    finally:
        run_systemctl("start", TIMER)
        emit(event="normal_timer_restored", live_due=due_count())


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        emit(event="stopped", reason=type(exc).__name__)
        raise
