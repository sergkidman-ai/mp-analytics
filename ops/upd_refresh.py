# поток: fin
"""Daily refresh of the existing WB redemption notices and UPD page."""
import fcntl
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from collectors import wb_documents
from reports import upd_page


def main():
    folder = wb_documents.STATE_DIR
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('UPD refresh already running'); return
        try:
            today = date.today()
            wb_documents.main_range(today - timedelta(days=90), today)
            warnings = upd_page.source_warnings()
            if warnings:
                raise RuntimeError("; ".join(warnings))
        finally:
            print(upd_page.render(), flush=True)


if __name__ == '__main__':
    main()
