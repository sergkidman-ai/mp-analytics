# поток: ret
"""Durable per-day/company/recipient WB QR receipts shared by report and followup.

Persist intent BEFORE sendDocument. Ambiguous transport failures remain uncertain:
Telegram has no idempotency key, so automatically repeating them could duplicate QR.
"""
import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path

from returns_bot import tg, wb_qr

DIRECTORY = Path(__file__).resolve().parents[1] / '.private/returns-wb-qr-delivery'


@contextmanager
def locked():
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = DIRECTORY / '.lock'
    lock.touch(mode=0o600, exist_ok=True)
    with lock.open('r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read(day):
    path = DIRECTORY / (day + '.json')
    return json.loads(path.read_text()) if path.exists() else {}


def key(account, chat_id):
    return account + ':' + str(chat_id)


def pending(account, chat_id, now=None):
    now = now or wb_qr.now_msk()
    return read(now.date().isoformat()).get(key(account, chat_id), {}).get('status') not in (
        'sent', 'sending', 'uncertain')


def send(account, chat_id, attachment, now=None):
    """Return sent/skipped/failed/uncertain; one network attempt under interprocess lock."""
    now = now or wb_qr.now_msk()
    day = now.date().isoformat()
    with locked():
        # Revalidate against current civil time after waiting for another sender.
        if (wb_qr.now_msk().date().isoformat() != day or
                not wb_qr.enabled_day(now) or wb_qr.current(now, account=account) is None):
            return 'skipped'
        path = DIRECTORY / (day + '.json')
        receipts = read(day)
        item = key(account, chat_id)
        previous = receipts.get(item, {}).get('status')
        if previous == 'sent':
            return 'skipped'
        if previous in ('sending', 'uncertain'):
            return 'uncertain'
        filename, data, caption, mime = attachment
        # Caller may have prepared an attachment before today's cache was replaced.
        import hashlib
        _, meta = wb_qr.current(now, account=account)
        if hashlib.sha256(data).hexdigest() != meta['sha256']:
            return 'skipped'
        receipt = {'status': 'sending', 'at': now.isoformat(), 'filename': filename}
        receipts[item] = receipt
        wb_qr.atomic(path, json.dumps(receipts).encode())
        try:
            ack = tg.send_document(chat_id, data, filename, caption, mime=mime, tries=1)
            if not isinstance(ack, dict) or type(ack.get('message_id')) is not int:
                raise RuntimeError('missing_telegram_receipt')
        except Exception as error:
            receipt.update(status='failed' if isinstance(error, tg.Rejected) else 'uncertain',
                           error=type(error).__name__)
            transport = re.fullmatch(r'sendDocument: transport failure \(([A-Za-z]+)\)', str(error))
            if transport:
                receipt['transport'] = transport[1]
        else:
            receipt.update(status='sent', message_id=ack['message_id'])
        wb_qr.atomic(path, json.dumps(receipts).encode())
        return receipt['status']
