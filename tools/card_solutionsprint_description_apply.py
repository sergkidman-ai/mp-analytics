#!/usr/bin/env python3
# поток: card
"""Frozen, authorized SolutionsPrint description delta for the night of October 11."""
import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.card_rapid_description_sync import apply, save, stamp, window

OUT = ROOT / 'docs/reports/card_solutionsprint_description_sync_2026-10-11'
IDS = {'24c408f3-016c-44e2-89b4-55e14c307047',
       '9263b7de-97bf-11f0-0a80-015900374bc3'}
CUTOFF = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)


def main():
    with (OUT / 'apply.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((OUT / 'plan.json').read_text())
        summary = json.loads((OUT / 'summary.json').read_text())
        held = set(json.loads((OUT / 'excluded_chip_review.json').read_text()))
        assert len(plan) == len({r['id'] for r in plan}) == 1194
        assert all(r['supplier_id'] in IDS and r['id'] not in held
                   and r['description'].strip() and '\xa0' not in r['description']
                   for r in plan)
        if (OUT / 'journal.jsonl').exists():
            raise RuntimeError('Prior journal exists; inspect readback before any resume')

        def guard():
            paused = False
            while True:
                now = datetime.now(timezone.utc)
                if now >= CUTOFF or not window():
                    raise RuntimeError('Authorized night window ended; stop')
                active = subprocess.run(['systemctl', 'is-active', '--quiet',
                                         'prc-enter-buyprice.service']).returncode == 0
                reserved = now.hour == 20 and 3 <= now.minute < 8
                if not active and not reserved:
                    if paused:
                        summary.update(status='starting' if not summary['writes'] else 'running', at=stamp())
                        save(OUT / 'summary.json', summary)
                        print(stamp(), 'Buyprice complete; continuing descriptions', flush=True)
                    return paused
                if not paused:
                    summary.update(status='waiting_for_buyprice', at=stamp())
                    save(OUT / 'summary.json', summary)
                    print(stamp(), 'Waiting for buyprice; no writes', flush=True)
                    paused = True
                time.sleep(30)

        # The common executor checks competing writers before its first batch.
        guard()
        apply(OUT, plan, summary, before_write=guard)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        data = json.loads((OUT / 'summary.json').read_text())
        data.update(status='stopped', at=stamp(), error_type=type(exc).__name__)
        save(OUT / 'summary.json', data)
        raise
