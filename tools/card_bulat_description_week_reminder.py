#!/usr/bin/env python3
# поток: card
"""User-authorized one-off Telegram task to Natalia, due one week after 09 Oct."""
import argparse
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path('/opt/mp-analytics')
OUT = ROOT / 'docs/reports/card_bulat_full_title_sync_2026-10-09'
TASK = OUT / 'weekly_check_task.json'
MARKER = OUT / 'weekly_check_sent.json'
INTENT = OUT / 'weekly_check_send_intent.json'


def save(path, value):
    with path.open('w') as fh:
        fh.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def send_due():
    with (OUT / 'weekly_check.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        task = json.loads(TASK.read_text())
        now = datetime.now(timezone.utc)
        if now < datetime.fromisoformat(task['due_utc']):
            print('Not due; no message sent')
            return
        if MARKER.exists():
            print('Already delivered; no duplicate message')
            return
        if INTENT.exists():
            raise RuntimeError('Previous send outcome uncertain; inspect before retry')
        load_dotenv(ROOT / '.env')
        token = os.environ.get(task['bot_env'])
        recipient = os.environ.get(task['recipient_env'])
        if not token or not recipient:
            raise RuntimeError('Natalia Telegram configuration missing')
        save(INTENT, {'at': now.isoformat(), 'recipient': 'Наталия', 'status': 'sending'})
        try:
            response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                                     json={'chat_id': recipient, 'text': task['text'],
                                           'disable_web_page_preview': True}, timeout=(15, 40))
        except requests.RequestException:
            raise RuntimeError('Telegram outcome uncertain; token withheld') from None
        if response.status_code != 200:
            raise RuntimeError(f'Telegram HTTP {response.status_code}; token withheld')
        data = response.json()
        if not data.get('ok'):
            raise RuntimeError('Telegram did not confirm delivery')
        save(MARKER, {'sent_at': datetime.now(timezone.utc).isoformat(),
                      'recipient': 'Наталия', 'message_id': data['result']['message_id'], 'ok': True})
        task.update({'status': 'sent', 'sent': True, 'sent_at': datetime.now(timezone.utc).isoformat()})
        save(TASK, task)
        print('Week-check task delivered to Natalia in Telegram')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-due', action='store_true')
    args = parser.parse_args()
    if args.send_due:
        send_due()
    else:
        task = json.loads(TASK.read_text())
        print(json.dumps({'recipient': 'Наталия', 'due_msk': task['due_msk'],
                          'text_chars': len(task['text']), 'sent': task['sent']}, ensure_ascii=False))
