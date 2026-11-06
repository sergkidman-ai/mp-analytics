# поток: inv
"""Bank-scoped launcher for existing collectors, watcher and sender; default: Sber."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path('/opt/mp-analytics')
PYTHON = ROOT / 'venv/bin/python'
ORG = '7811803918'
BANK_ORGS = {'sber': ORG, 'alfa': '7807355364'}


def commands(mode, bank='sber'):
    org = BANK_ORGS[bank]
    sync = [str(PYTHON), str(ROOT / 'run_inv.py'), '--' + bank + '-only']
    watch = [str(PYTHON), str(ROOT / 'invoice_bot/po_payment_watch.py'), '--org', org]
    send = [str(PYTHON), str(ROOT / 'invoice_bot/payment_autosend.py'), '--org', org, '--no-tg']
    return {
        'sync': [sync + ['--today'], watch + ['--close-only']],
        'prepare': [sync + ['--days', '3'], watch],
        'morning': [send + ['--kinds', 'advance,deferred_batch']],
        'evening': [sync + ['--today'], watch + ['--methods', 'prepayment_per_order'],
                    send + ['--kinds', 'prepayment_order']],
    }[mode]


def main(mode, bank='sber'):
    if bank not in BANK_ORGS or (bank != 'sber' and mode == 'secret'):
        raise ValueError('Unsupported bank schedule mode')
    os.umask(0o077)
    lock_path = '/run/lock/mp-inv-diskver.lock' if bank == 'sber' else '/run/lock/mp-inv-alfa.lock'
    with Path(lock_path).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mode == 'secret':
            sys.path.insert(0, str(ROOT))
            from ops import sber_secret_rotate as rotate
            rotate.tg = lambda message: print('Sber secret maintenance notification recorded in journal')
            before = hashlib.sha256((ROOT / '.env').read_bytes()).digest()
            try:
                code = rotate.cron()
            except Exception as error:
                print('Sber secret maintenance failed: ' + type(error).__name__)
                return 1
            if before != hashlib.sha256((ROOT / '.env').read_bytes()).digest():
                subprocess.run(['systemctl', 'try-restart', 'mp-dashboard'], check=True)
            return code
        for command in commands(mode, bank):
            result = subprocess.run(command, cwd=ROOT)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('sync', 'prepare', 'morning', 'evening', 'secret'))
    parser.add_argument('--bank', choices=tuple(BANK_ORGS), default='sber')
    args = parser.parse_args()
    raise SystemExit(main(args.mode, args.bank))
