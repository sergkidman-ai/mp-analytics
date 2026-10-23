# поток: fin
"""One-off catch-up of the due customer-order cohort through the normal oneshot unit.

The ordinary timer stays enabled. Each extra start uses the deployed runtime's
existing MS lock, 100-GET budget, 240-second deadline and five-order batch.
"""
import fcntl
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import db
from customer_guard.domain import ACTIVE

UNIT = "customer-order-guard.service"
LOCK = Path("/opt/mp-analytics/runtime/customer-order-catchup.lock")


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def active_state():
    return subprocess.check_output(
        ["systemctl", "show", UNIT, "-p", "ActiveState", "--value"], text=True
    ).strip()


def cohort(now):
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


def rate_limit_count():
    rows = db.query("SELECT value FROM ms_shipment_metrics WHERE name='ms_429'")
    return rows[0]["value"] if rows else 0


def main():
    with LOCK.open("a+") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = datetime.now(timezone.utc)
        ids = cohort(started)
        original = len(ids)
        baseline_429 = rate_limit_count()
        emit(event="start", due_cohort=original, started_at=started)
        if not ids:
            return
        stalled = 0
        previous = original
        for run in range(1, 301):
            if datetime.now(timezone.utc)-started > timedelta(hours=12):
                raise RuntimeError("catchup_wall_budget")
            while active_state() not in ("inactive", "failed"):
                time.sleep(5)
            if active_state() == "failed":
                raise RuntimeError("guard_unit_failed")
            subprocess.run(["systemctl", "start", UNIT], check=True, timeout=660)
            current = remaining(ids, started)
            status = db.query("SELECT last_error FROM customer_guard_runtime WHERE id")[0]
            emit(event="pass", run=run, remaining=current, cleared=original-current,
                 runtime_error=status["last_error"])
            if current == 0:
                emit(event="complete", runs=run, cohort=original)
                return
            if rate_limit_count() > baseline_429:
                raise RuntimeError("ms_rate_limited")
            stalled = stalled + 1 if current >= previous else 0
            if stalled >= 3:
                raise RuntimeError("catchup_no_progress")
            previous = current
            time.sleep(8)
        raise RuntimeError("catchup_run_budget")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit(event="stopped", reason=type(exc).__name__)
        raise
