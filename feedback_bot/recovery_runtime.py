# поток: rev
"""Durable Telegram input handling and restoration publication barriers."""
import copy
import html
import os
import fcntl
import time
from contextlib import contextmanager
from psycopg2.extras import Json
from core import db


@contextmanager
def processing_lock(path='/root/rev-recovery/cycle.lock'):
    """Serialize processing with the short tick, without holding it during API collection."""
    started=time.monotonic()
    with open(path,'a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:
            yield time.monotonic()-started
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)


@contextmanager
def card_lock(mod_id):
    if mod_id is None:
        yield
        return
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ('rev_card:' + str(mod_id),))
            yield


def setup():
    db.execute('ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_revision text')
    db.execute('ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_sent_card_ok boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE feedback_moderation ADD COLUMN IF NOT EXISTS recovery_edit_card_ok boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS recovery_active_unanswered boolean')
    db.execute('ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS recovery_status_checked_at timestamptz')
    db.execute('''CREATE TABLE IF NOT EXISTS rev_telegram_inbox (
        update_id bigint PRIMARY KEY, payload jsonb NOT NULL,
        state text NOT NULL DEFAULT 'pending', received_at timestamptz DEFAULT now(),
        handled_at timestamptz, error_type text)''')
    db.execute('''CREATE TABLE IF NOT EXISTS rev_telegram_checkpoint (
        id int PRIMARY KEY CHECK(id=1), next_offset bigint NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS rev_edit_session (
        user_id text PRIMARY KEY, mod_id bigint NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE TABLE IF NOT EXISTS rev_publication_attempt (
        platform text, account text, kind text, ext_id text,
        state text NOT NULL, final_text text NOT NULL, created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now(), PRIMARY KEY(platform,account,kind,ext_id))''')


def publication_block():
    if (os.getenv('FEEDBACK_PUBLISHER_FENCED', '0') != '1'
            and os.getenv('FEEDBACK_OLD_VPS_OVERRIDE', '0') != '1'):
        return 'Не исключён второй отправщик со старого VPS'
    if os.getenv('FEEDBACK_LIVE_SEND', '0') != '1':
        return 'Публикации выключены в конфигурации'
    return ''


def callback_prefix():
    epoch = os.environ.get('FEEDBACK_CALLBACK_EPOCH')
    if not epoch:
        raise RuntimeError('Missing FEEDBACK_CALLBACK_EPOCH')
    return 'rev:' + epoch + ':'


def scoped_markup(markup):
    result = copy.deepcopy(markup)
    prefix = callback_prefix()
    for row in result.get('inline_keyboard', []):
        for button in row:
            data = button.get('callback_data')
            if data and not data.startswith(prefix):
                button['callback_data'] = prefix + data
    return result


class DurableEdits:
    def __setitem__(self, user_id, mod_id):
        db.execute('''INSERT INTO rev_edit_session(user_id,mod_id) VALUES(%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET mod_id=EXCLUDED.mod_id,updated_at=now()''',
            (str(user_id), mod_id))

    def pop(self, user_id, default=None):
        # Keep the session until the received text has been durably saved.
        rows = db.query('SELECT mod_id FROM rev_edit_session WHERE user_id=%s', (str(user_id),))
        return rows[0]['mod_id'] if rows else default

    def finish(self, user_id):
        db.execute('DELETE FROM rev_edit_session WHERE user_id=%s', (str(user_id),))


def refresh_edit_cards(edit_text, keyboard):
    updated=0
    for row in db.query("SELECT id FROM feedback_moderation WHERE state='edited' AND final_text IS NOT NULL AND NOT recovery_edit_card_ok"):
        with card_lock(row['id']):
            rows=db.query("SELECT * FROM feedback_moderation WHERE id=%s AND state='edited' AND NOT recovery_edit_card_ok",(row['id'],))
            if not rows:
                continue
            record=rows[0]
            response=edit_text(record['tg_chat_id'],record['tg_msg_id'],
                '✏️ <b>Правка принята. Завершаем отправку.</b>\n\n'
                +html.escape(record['final_text'][:3000]),reply_markup={'inline_keyboard':[]})
            if response and response.get('ok'):
                db.execute('UPDATE feedback_moderation SET recovery_edit_card_ok=true WHERE id=%s',(record['id'],))
                updated+=1
    return updated


def poll_once(api, callback, message):
    rows = db.query('SELECT next_offset FROM rev_telegram_checkpoint WHERE id=1')
    params = {'timeout': 20, 'limit': 100}
    if rows:
        params['offset'] = rows[0]['next_offset']
    response = api('getUpdates', params, timeout=30)
    if not response.get('ok'):
        raise RuntimeError('Telegram getUpdates rejected')
    for update in response.get('result', []):
        db.execute('INSERT INTO rev_telegram_inbox(update_id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                   (update['update_id'], Json(update)))
    for row in db.query("SELECT update_id,payload FROM rev_telegram_inbox WHERE state='pending' ORDER BY update_id"):
        update = row['payload']
        try:
            if 'callback_query' in update:
                cb = update['callback_query']
                sid = cb.get('data', '').rsplit(':', 1)[-1].split('.')[0]
                with card_lock(int(sid) if sid.isdigit() else None):
                    callback(cb)
            elif 'message' in update:
                # The verified challenge is retained in the inbox, not treated as an edit.
                text = update['message'].get('text', '')
                if text != 'REV-CLOUD001-0311-SERGEY-4827':
                    uid = str(update['message'].get('from', {}).get('id', ''))
                    edits = db.query('SELECT mod_id FROM rev_edit_session WHERE user_id=%s', (uid,))
                    with card_lock(edits[0]['mod_id'] if edits else None):
                        message(update['message'])
        except Exception as error:
            db.execute('UPDATE rev_telegram_inbox SET error_type=%s WHERE update_id=%s',
                       (type(error).__name__, row['update_id']))
            raise
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE rev_telegram_inbox SET state='handled',handled_at=now(),error_type=NULL WHERE update_id=%s", (row['update_id'],))
                cur.execute('''INSERT INTO rev_telegram_checkpoint(id,next_offset) VALUES(1,%s)
                    ON CONFLICT(id) DO UPDATE SET next_offset=EXCLUDED.next_offset''', (row['update_id'] + 1,))


def claim_publication(row, text):
    return bool(db.execute('''INSERT INTO rev_publication_attempt(platform,account,kind,ext_id,state,final_text)
        VALUES(%s,%s,%s,%s,'inflight',%s) ON CONFLICT DO NOTHING''',
        (row['platform'],row['account'],row['kind'],row['ext_id'],text)))


def publication_result(row, state):
    db.execute('''UPDATE rev_publication_attempt SET state=%s,updated_at=now()
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s''',
        (state,row['platform'],row['account'],row['kind'],row['ext_id']))
