# поток: fin
"""One local price-writer lock shared by WB guard, plan_raise and auto entry.

Unresolved asynchronous price uploads additionally leave an account barrier on disk.
Do not remove that barrier manually while an upload can still change prices.
"""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get('WB_PRICE_RUNTIME', '/opt/mp-analytics/runtime/wb_auto_entry'))


@contextmanager
def writer_lock(name='writer'):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / (name + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def barrier(account):
    return ROOT / (account + '.pending')


def locked_release(release):
    """Serialize WB quarantine only; Ozon/Market processing remains independent."""
    from functools import wraps
    @wraps(release)
    def wrapped(account, *args, **kwargs):
        try:
            with writer_lock():
                if barrier(account).exists():
                    return 'ОТЛОЖЕН: загрузка WB ещё не завершена', 0, None
            # Each WB push owns a bounded durable writer lock. Do not hold it while
            # walking an entire quarantine ladder or waiting on unrelated reads.
            return release(account, *args, **kwargs)
        except BlockingIOError:
            return 'ОТЛОЖЕН: другой процесс меняет цены WB', 0, None
    return wrapped


def legacy_main(main):
    """Serialize CLI price writers; pending uploads must settle before another writer."""
    import sys
    if '--apply' not in sys.argv or '--dry' in sys.argv or '--baseline' in sys.argv:
        return main()
    try:
        with writer_lock():
            pass
        # Legacy collection may take minutes. Each actual push acquires this lock
        # again; read-only analytics must not starve the scheduled floor controller.
        return main()
    except BlockingIOError:
        print('WB price writer: another writer is running; apply deferred', flush=True)
        return 75
