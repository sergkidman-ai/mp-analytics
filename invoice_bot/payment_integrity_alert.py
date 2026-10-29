# поток: inv
"""Alert Natalia in PRC when supplier payment preparation stops on org/store mismatch."""
import fcntl
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / '.private' / 'payment_integrity_alerts.json'
MSK = ZoneInfo('Europe/Moscow')
ORG_NAMES = {'7807355364': 'Цифровой', '7811803918': 'Дисквер'}
ASSIGNMENT_ERRORS = (
    'У документа отсутствует организация или склад',
    'организация заказа отличается от плательщика',
    'для организации требуется склад',
    'организация приёмки отличается от заказа',
    'склад приёмки отличается от заказа/организации',
)


def is_assignment_error(reason):
    return any(phrase in str(reason) for phrase in ASSIGNMENT_ERRORS)


def _send(message):
    token = os.getenv('TG_PRC_BOT_TOKEN', '').strip()
    recipient = os.getenv('TG_NATALIA_TASK_CHAT_ID', '').strip()
    if not token or not recipient.isdecimal() or int(recipient) <= 0:
        print('payment integrity alert: PRC token or Natalia recipient missing', flush=True)
        return False
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': recipient, 'text': message, 'disable_web_page_preview': True},
            timeout=25,
        )
        response.raise_for_status()
        if not response.json().get('ok'):
            raise RuntimeError('telegram_rejected')
    except Exception as exc:
        # Request errors may include the token-bearing URL; never print their text.
        print('payment integrity alert: delivery failed: ' + type(exc).__name__, flush=True)
        return False
    return True


def notify_assignment(org_inn, supplier, reason, *, ledger=LEDGER, now=None):
    """Send once per MSK day and exact reason; failed deliveries remain retryable."""
    if not is_assignment_error(reason):
        return False
    today = (now or datetime.now(MSK)).astimezone(MSK).date().isoformat()
    key = json.dumps([today, str(org_inn), str(supplier), str(reason)], ensure_ascii=False)
    ledger = Path(ledger)
    ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(str(ledger) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            sent = json.loads(ledger.read_text()) if ledger.exists() else {}
            if not isinstance(sent, dict):
                sent = {}
        except (ValueError, OSError):
            sent = {}
        if key in sent:
            return False
        message = (
            '⚠️ Подготовка оплаты остановлена\n'
            f'Организация: {ORG_NAMES.get(str(org_inn), str(org_inn))}\n'
            f'Поставщик: {supplier}\n'
            f'Причина: {reason}\n'
            'Проверьте организацию и склад заказа поставщику и приёмки в МойСклад.'
        )
        if not _send(message):
            return False
        cutoff = (datetime.fromisoformat(today).date() - timedelta(days=14)).isoformat()
        sent = {k: v for k, v in sent.items() if v >= cutoff}
        sent[key] = today
        tmp = ledger.with_suffix('.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(sent, stream, ensure_ascii=False)
        os.replace(tmp, ledger)
        print('payment integrity alert: sent to Natalia in PRC', flush=True)
        return True
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
