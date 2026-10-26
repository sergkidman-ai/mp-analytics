# поток: ret
"""Send only today's late WB QR, without repeating marketplace collection or report."""
import argparse
from collections import Counter

from returns_bot import tg, wb_qr, wb_qr_delivery


def run(dry_run=False, now=None):
    now = now or wb_qr.now_msk()
    counts = Counter()
    # Morning report normally finishes by 09:10. Before 09:15 it owns delivery.
    if not wb_qr.enabled_day(now) or (now.hour, now.minute) < (9, 15):
        return dict(counts)
    for account in wb_qr.enabled_accounts():
        attachment = wb_qr.attachment(now, account=account)
        if not attachment:
            counts['missing'] += 1
            continue
        for target in dict.fromkeys(tg.notify_ids()):
            if dry_run:
                counts['pending' if wb_qr_delivery.pending(account, target, now) else 'skipped'] += 1
            else:
                counts[wb_qr_delivery.send(account, target, attachment, now)] += 1
    print('WB QR followup:', dict(counts), flush=True)
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    counts = run(args.dry_run)
    return int(bool(counts.get('failed') or counts.get('uncertain')))


if __name__ == '__main__':
    raise SystemExit(main())
