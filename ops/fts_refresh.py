# поток: fin
"""Refresh the existing FTS report independently of the full financial pipeline."""
import fcntl
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from core import db
from collectors import ozon_orders_report as orders, ozon_postings as postings
from reports import fts_page


def main(period=None):
    period = period or (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    folder = BASE / '.private' / 'fts_sources'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('FTS refresh already running'); return
        try:
            orders.main_period(period)
            end = (period.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            missing = db.query("""
                SELECT r.account, r.delivery_schema, r.payload->>'processed_at' processed_at
                FROM raw_ozon_orders_report r LEFT JOIN raw_ozon_posting p
                  ON p.account=r.account AND p.posting_number=r.posting_number
                    AND p.scheme=r.delivery_schema
                WHERE lower(r.status)='доставлен' AND r.delivered_at BETWEEN %s AND %s
                  AND coalesce(p.payload->'financial_data'->>'cluster_to','')=''
            """, (period, end))
            windows = set()
            for row in missing:
                processed = orders._parse_date(row['processed_at'])
                if processed is None:
                    raise RuntimeError('Missing processed_at: cannot restore delivery country safely')
                windows.add((row['account'], row['delivery_schema'], processed.replace(day=1)))
            for account, scheme, start in sorted(windows):
                finish = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                raw = postings.fetch_postings(account, scheme, start.isoformat(), finish.isoformat())
                postings.load_raw(account, raw, scheme, start, finish)
            from reports import fts_report
            data = fts_report.build(period)
            if data['source_warnings']:
                raise RuntimeError('; '.join(data['source_warnings']))
            print(data['totals'], flush=True)
        finally:
            # A failed source refresh must remain visibly incomplete.
            fts_page.render(period)


if __name__ == '__main__':
    selected = date.fromisoformat(sys.argv[1] + '-01') if len(sys.argv) > 1 else None
    main(selected)
