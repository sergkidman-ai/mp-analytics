# поток: fin
"""Refresh current/previous month finance sources in existing recovery staging."""
import calendar
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from core import db
from tools import bi_history_restore as recovery

ROOT = Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))/'sources'
STAGE = 'bi_history_20261104'


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT/'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db.DATABASE_URL = recovery.STAGE_DSN
        os.environ['DATABASE_URL'] = db.DATABASE_URL
        assert db.query('SELECT current_database() name')[0]['name'] == STAGE
        recovery.ROOT = ROOT
        recovery.END = dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()))
        recovery.AS_OF = recovery.END + dt.timedelta(days=1)
        current=recovery.END.replace(day=1)
        previous=(current-dt.timedelta(days=1)).replace(day=1)
        recovery.MONTHS = [previous.strftime('%Y-%m'),current.strftime('%Y-%m')]
        recovery.STATE = (json.loads((ROOT/'checkpoint.json').read_text())
                          if (ROOT/'checkpoint.json').exists() else {'tasks': {}})
        recovery.STATE.update(database=STAGE, source_until=recovery.END.isoformat())
        recovery.save()
        jobs = [('wb_acc1', recovery.wb_lane, 'wb_acc1'),
                ('wb_acc2', recovery.wb_lane, 'wb_acc2'),
                ('oz_acc1', recovery.ozon_lane, 'oz_acc1'),
                ('oz_acc2', recovery.ozon_lane, 'oz_acc2'),
                ('yandex_orders', lambda _: recovery.yandex_lane(), None)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn, arg): name for name, fn, arg in jobs}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    recovery.emit('source_lane_done', lane=name)
                except Exception as error:
                    recovery.mark('lane/'+name, status='failed', error=type(error).__name__)
        recovery.adapt_ozon()
        from collectors import yandex_services as services
        original = services._services_report
        for i, month in enumerate(recovery.MONTHS):
            key = 'yandex_services/'+month
            if recovery.completed(key):
                continue
            if i:
                time.sleep(130)
            try:
                y, m = map(int, month.split('-'))
                last = min(recovery.END.isoformat(), f'{month}-{calendar.monthrange(y,m)[1]:02d}')
                csvs = original(month+'-01', last, timeout=240)
                services._services_report = lambda *a, **k: csvs
                count = services.collect_api(months=[month+'-01'])
                recovery.mark(key, status='done', rows=count, first=month+'-01', last=last)
            except Exception as error:
                recovery.mark(key, status='failed', error=type(error).__name__)
            finally:
                services._services_report = original
        recovery.STATE['phase'] = 'sources_refreshed_review_required'
        recovery.save()
        recovery.emit('source_refresh_complete', tasks=len(recovery.STATE['tasks']))


if __name__ == '__main__':
    main()
