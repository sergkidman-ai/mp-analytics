#!/usr/bin/env python3
# поток: card
"""User-authorized immediate Profiline description update, frozen 623 IDs."""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.card_rapid_description_sync import apply, save, stamp, log, MSK
from tools.card_profiline_description_prepare import IDS
from core import ms_api

OUT = ROOT / 'docs/reports/card_profiline_description_prepare_2026-10-09'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    with (OUT / 'apply.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((OUT / 'plan.json').read_text())
        summary = json.loads((OUT / 'summary.json').read_text())
        assert len(plan) == 623 and len({r['id'] for r in plan}) == 623
        assert all(r['supplier_id'] in IDS and r['status'] == 'READY' for r in plan)
        if args.resume:
            events = [json.loads(x) for x in (OUT / 'journal.jsonl').read_text().splitlines()]
            sent = {i for e in events if e.get('phase') == 'sending' for i in e['ids']}
            verified = {e['id'] for e in events if e.get('phase') == 'verified'}
            assert len(sent) == 200 and len(verified) == 179 and verified <= sent
            frozen = {r['id']: r for r in plan}
            assert sent <= set(frozen)
            for i in sorted(sent - verified):
                live = ms_api.get('/entity/product/' + i)
                row = frozen[i]
                if (live.get('archived') or ms_api.meta_id(live, 'supplier') != row['supplier_id']
                        or live.get('article') != row['article']
                        or live.get('externalCode', '') != row['external_code']
                        or (live.get('description') or '').strip() != row['description'].strip()):
                    raise RuntimeError('Previously submitted card differs; stop without rewriting')
                log(OUT, {'phase': 'verified', 'id': i, 'recovered_by_readback': True,
                          'edge_whitespace_normalized': (live.get('description') or '') != row['description']})
                time.sleep(.2)
            plan = [{**r, 'description': r['description'].strip()} for r in plan if r['id'] not in sent]
            assert len(plan) == 423
            save(OUT / 'resume_423_plan.json', plan)
            log(OUT, {'phase': 'resume', 'excluded_prior_ids': sorted(sent),
                      'remaining_ids': [r['id'] for r in plan]})
            summary.update(changes=623, writes=200, verified=200, status='starting',
                           remaining_at_resume=423, explicit_resume_authorization='User: complete 423 now',
                           write_fields=['description'])
            summary.pop('error_type', None)
        else:
            assert not (OUT / 'journal.jsonl').exists()
            summary.update(changes=623, verified=0, status='starting',
                           authorized_start='User explicitly requested immediate update of 623 descriptions',
                           write_fields=['description'])
        save(OUT / 'summary.json', summary)
        started = datetime.now(timezone.utc)
        local = started.astimezone(MSK)
        end_date = local.date() if local.hour < 5 else local.date() + timedelta(days=1)
        cutoff = datetime.combine(end_date, datetime.min.time(), MSK).replace(hour=5).astimezone(timezone.utc)

        def guard():
            paused = False
            while True:
                now = datetime.now(timezone.utc)
                if now >= cutoff:
                    raise RuntimeError('05:00 MSK cutoff; stop')
                active = subprocess.run(['systemctl', 'is-active', '--quiet',
                                         'prc-enter-buyprice.service']).returncode == 0
                # Reserve two minutes before scheduled 23:05 MSK job, including timer jitter.
                reserved = (now.hour == 20 and 3 <= now.minute < 8)
                if not active and not reserved:
                    if paused:
                        summary.update(status='running', at=stamp())
                        save(OUT / 'summary.json', summary)
                        print(stamp(), 'Resuming after buyprice window', flush=True)
                    return paused
                if not paused:
                    summary.update(status='waiting_for_buyprice', at=stamp())
                    save(OUT / 'summary.json', summary)
                    print(stamp(), 'Waiting for buyprice job; no concurrent writes', flush=True)
                    paused = True
                time.sleep(30)

        apply(OUT, plan, summary, allow_daytime=True, before_write=guard,
              resume=args.resume, fresh_canary=args.resume)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        p = OUT / 'summary.json'
        data = json.loads(p.read_text())
        data.update(status='failed', at=stamp(), error_type=type(exc).__name__)
        save(p, data)
        raise
