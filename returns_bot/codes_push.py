# поток: ret
"""Weekday 10:00 MSK code-only delivery. No order reports or collection.

Ozon send intents are durable; uncertain deliveries are never retried automatically.
WB uses the shared per-day/account/chat ledger, including hourly late delivery.
"""
import argparse
import fcntl
import json
from collections import Counter
from pathlib import Path

from returns_bot import bot, tg, wb_qr, wb_qr_delivery
from returns_bot.sources import ozon

DIRECTORY = Path(__file__).resolve().parents[1] / '.private/returns-ozon-code-delivery'


def send_ozon(account, target, attachment, now):
    if target not in tg.codes_only_notify_ids(now):
        return 'skipped'
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = DIRECTORY / '.lock'
    lock.touch(mode=0o600, exist_ok=True)
    with lock.open('r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        path = DIRECTORY / (now.date().isoformat() + '.json')
        receipts = json.loads(path.read_text()) if path.exists() else {}
        key = account + ':' + str(target)
        previous = receipts.get(key, {}).get('status')
        if previous == 'sent':
            return 'skipped'
        if previous in ('sending', 'uncertain'):
            return 'uncertain'
        if wb_qr.now_msk().date() != now.date():
            return 'skipped'
        filename, data, caption, mime = attachment
        receipt = {'status': 'sending', 'at': now.isoformat(), 'filename': filename}
        receipts[key] = receipt
        wb_qr.atomic(path, json.dumps(receipts).encode())
        try:
            ack = tg.send_document(target, data, filename, caption, mime=mime, tries=1)
            if not isinstance(ack, dict) or type(ack.get('message_id')) is not int:
                raise RuntimeError('missing_telegram_receipt')
        except Exception as error:
            receipt.update(status='failed' if isinstance(error, tg.Rejected) else 'uncertain',
                           error=type(error).__name__)
        else:
            receipt.update(status='sent', message_id=ack['message_id'])
        wb_qr.atomic(path, json.dumps(receipts).encode())
        return receipt['status']


def run(dry_run=False, now=None):
    now = now or wb_qr.now_msk()
    targets = tg.codes_only_notify_ids(now)
    counts = Counter()
    if not targets or (now.hour, now.minute) < (10, 0):
        return dict(counts)
    # Dry preview must not request new barcodes or print private codes.
    if dry_run:
        return {'recipients': len(targets), 'ozon_accounts': len(ozon.CRED_ENV),
                'wb_accounts': len(wb_qr.enabled_accounts())}
    for account in ozon.CRED_ENV:
        try:
            attachments = bot.barcode_files(account)
            if not attachments:
                counts['failed'] += 1
            for attachment in attachments:
                for target in targets:
                    counts[send_ozon(account, target, attachment, now)] += 1
        except Exception as error:
            print('Ozon code unavailable:', account, type(error).__name__)
            counts['failed'] += 1
    for account in wb_qr.enabled_accounts():
        attachment = wb_qr.attachment(now, account=account)
        if not attachment:
            counts['missing_wb'] += 1
            continue  # The hourly followup and upload callback deliver it once available.
        for target in targets:
            counts[wb_qr_delivery.send(account, target, attachment, now)] += 1
    print('10:00 code-only delivery:', dict(counts), flush=True)
    return dict(counts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    result = run(dry_run=args.dry_run)
    if args.dry_run:
        print('Code-only delivery preview:', result)
    return int(bool(result.get('failed') or result.get('uncertain')))


if __name__ == '__main__':
    raise SystemExit(main())
