# поток: fin
"""Read-only observation of the already running scheduled financial calculation."""
import json
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
LOG = OUT.parents[2] / 'run_daily.log'
START = '[run_daily] старт 2026-10-20 19:07'


def main():
    evidence, completed = [], set()
    deadline = time.monotonic() + 2400
    position = 0
    started = False
    last_notice = 0
    while time.monotonic() < deadline:
        with LOG.open(errors='replace') as stream:
            stream.seek(position)
            for raw in stream:
                line = raw.rstrip()
                if START in line:
                    started = True
                    evidence = []
                    completed = set()
                if not started:
                    continue
                if any(tag in line for tag in ('[ok]', '[FAIL]', '[skip]', '[run_daily]')):
                    evidence.append(line)
                    if 'Ozon' in line or '[run_daily]' in line:
                        print(line[:240], flush=True)
                    for account in ('oz_acc1', 'oz_acc2'):
                        if f'[ok] Ozon маржа {account} 2026-10-01' in line:
                            completed.add(account)
                    (OUT / 'scheduled_run_events.txt').write_text('\n'.join(evidence)+'\n')
                if '[run_daily] готово' in line or '[run_daily] завершено' in line:
                    if len(completed) != 2:
                        (OUT/'monitor_result.json').write_text(json.dumps(
                            {'status':'finished_without_both_october_calculations',
                             'completed':sorted(completed)},indent=2))
                        return
            position = stream.tell()
        if len(completed) == 2:
            (OUT/'monitor_result.json').write_text(json.dumps(
                {'status':'both_october_calculations_succeeded',
                 'completed':sorted(completed)},indent=2))
            print('Both October Ozon calculations completed.',flush=True)
            return
        if time.monotonic()-last_notice > 55:
            print('Waiting for scheduled Ozon stage; no duplicate calculation started.',flush=True)
            last_notice = time.monotonic()
        time.sleep(5)
    (OUT/'monitor_result.json').write_text(json.dumps(
        {'status':'observation_timeout','completed':sorted(completed)},indent=2))


if __name__=='__main__':
    main()
