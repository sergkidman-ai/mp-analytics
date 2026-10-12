# поток: ev
"""Persistent per-action acknowledgements in @ds_prc_bot; no marketplace writes."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
import requests
from psycopg2.extras import RealDictCursor

MSK = timezone(timedelta(hours=3))
PREFIX = 'wbpromo_done:'
AUTO_SUFFIX = re.compile(r'\s*\((?:автоматическ|автоматическая акция)[^)]*\)\s*$', re.I)
ACC_NAME = {'wb_acc1': 'Цифровой квадрат', 'wb_acc2': 'Дисквэр'}


def recipient():
    # Explicit Natalia destination; generic notification lists are not recipients here.
    return int(os.environ.get('TG_PRC_NATALIA_ID', '1231747786'))


def api(method, payload, timeout=20):
    token = os.environ.get('TG_PRC_BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('TG_PRC_BOT_TOKEN is missing')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/{method}',
                                 json=payload, timeout=timeout)
        result = response.json()
    except (requests.RequestException, ValueError):
        # requests exceptions can include the secret URL. Never log them.
        raise RuntimeError(f'Telegram {method}: transport failure') from None
    if not response.ok or not result.get('ok'):
        raise RuntimeError(f'Telegram {method}: error {result.get("error_code", response.status_code)}')
    return result.get('result')


def group_key(name, start, end):
    clean = AUTO_SUFFIX.sub('', name).strip()
    canonical = re.sub(r'\s+', ' ', clean.casefold().replace('ё', 'е'))
    return hashlib.sha256(json.dumps([canonical, str(start), str(end)],
                                    ensure_ascii=False).encode()).hexdigest()


def local_date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(MSK).date() if value else None


def discover(conn, rows, target, include_notified=False):
    """Refresh notices and queue only new due actions; never backfill historical notices."""
    groups = {}
    with conn.cursor() as cur:
        for acc, promo in rows:
            pid = promo.get('id')
            if pid is None:
                continue
            name = (promo.get('name') or '').strip()
            kind = (promo.get('type') or '').lower()
            start, end = local_date(promo.get('startDateTime')), local_date(promo.get('endDateTime'))
            cur.execute('''INSERT INTO wb_promo_notice(account,promo_id,promo_type,name,starts_at,ends_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(account,promo_id) DO UPDATE
                SET name=EXCLUDED.name,promo_type=EXCLUDED.promo_type,
                    starts_at=EXCLUDED.starts_at,ends_at=EXCLUDED.ends_at
                RETURNING notified_at''',
                (acc,pid,kind,name,promo.get('startDateTime'),promo.get('endDateTime')))
            sent = cur.fetchone()[0]
            if kind != 'auto' or start != target:
                continue
            key = group_key(name,start,end)
            g = groups.setdefault(key, dict(name=AUTO_SUFFIX.sub('',name).strip(), start=start,
                end=end, accounts=set(), refs=[], fresh=False))
            g['accounts'].add(acc);g['refs'].append(dict(account=acc,promo_id=pid))
            g['fresh'] |= sent is None or include_notified
        for key, g in groups.items():
            if not g['fresh']:
                continue
            cur.execute('''INSERT INTO wb_promo_task(group_key,name,starts_on,ends_on,accounts,promo_refs,recipient_id)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(group_key) DO UPDATE
                SET accounts=(SELECT ARRAY(SELECT DISTINCT unnest(wb_promo_task.accounts||EXCLUDED.accounts))),
                    promo_refs=(SELECT jsonb_agg(DISTINCT value) FROM
                        jsonb_array_elements(wb_promo_task.promo_refs||EXCLUDED.promo_refs) AS e(value))''',
                (key,g['name'],g['start'],g['end'],sorted(g['accounts']),json.dumps(g['refs']),recipient()))
    return len([g for g in groups.values() if g['fresh']])


def notification(task, today=None):
    today = today or datetime.now(MSK).date()
    start = task['starts_on']
    if start == today + timedelta(days=1):
        heading = f'📣 Завтра ({start:%d.%m}) стартует автоакция WB'
    elif start == today:
        heading = f'📣 Сегодня ({start:%d.%m}) стартует автоакция WB'
    elif start < today:
        heading = f'🔔 Напоминание об автоакции WB, старт {start:%d.%m}'
    else:
        heading = f'📣 Автоакция WB стартует {start:%d.%m}'
    end = task['ends_on']
    period = f'{start:%d.%m} → {end:%d.%m}' if end else f'Старт: {start:%d.%m}'
    accounts = ', '.join(ACC_NAME.get(a,a) for a in task['accounts'])
    return '\n'.join([heading,'',task['name'][:2500],period,accounts,'',
        'После обработки акции нажмите «Сделано». До подтверждения напоминание будет приходить каждый час.'])


def send_due(conn, dry=False):
    sent = 0
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Cron runs at :41 hourly. Hour buckets avoid skipping the next hour due to API latency.
        cur.execute('''SELECT * FROM wb_promo_task WHERE done_at IS NULL AND
            (last_sent_at IS NULL OR last_sent_at < date_trunc('hour',now()))
            ORDER BY id FOR UPDATE SKIP LOCKED''')
        for task in cur.fetchall():
            if dry:
                print(f"pending #{task['id']}: {task['name'][:100]}")
                continue
            # Row lock serializes send vs acknowledgement and concurrent cron jobs.
            try:
                result = api('sendMessage', dict(chat_id=task['recipient_id'],text=notification(task),
                    reply_markup=dict(inline_keyboard=[[dict(text='Сделано',callback_data=f"{PREFIX}{task['id']}")]])))
                message_id = int(result['message_id'])
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                print(f"task #{task['id']}: notification failed ({type(exc).__name__})",flush=True)
                continue
            cur.execute('''UPDATE wb_promo_task SET last_sent_at=now(),last_message_id=%s,
                send_count=send_count+1 WHERE id=%s''',(message_id,task['id']))
            for ref in task['promo_refs']:
                cur.execute('''UPDATE wb_promo_notice SET notified_at=coalesce(notified_at,now())
                    WHERE account=%s AND promo_id=%s''',(ref['account'],ref['promo_id']))
            sent += 1
    return sent


def handle_callback(conn, callback):
    """Persist completion before any UI call. Old reminder buttons remain idempotent."""
    data = callback.get('data','')
    if not isinstance(data,str) or not re.fullmatch(re.escape(PREFIX)+r'[0-9]+',data):
        return False
    task_id = int(data[len(PREFIX):])
    message = callback.get('message') or {}
    chat = (message.get('chat') or {}).get('id')
    user = (callback.get('from') or {}).get('id')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT recipient_id,done_at FROM wb_promo_task WHERE id=%s FOR UPDATE',(task_id,))
        task = cur.fetchone()
        allowed = bool(task and user == task['recipient_id'] == chat)
        if allowed:
            cur.execute('''UPDATE wb_promo_task SET done_at=coalesce(done_at,now()),
                done_by=coalesce(done_by,%s) WHERE id=%s''',(user,task_id))
    conn.commit()
    text = '✅ Сделано. Напоминания по этой акции остановлены.' if allowed else 'Подтверждение недоступно.'
    try:
        api('answerCallbackQuery',dict(callback_query_id=callback['id'],text=text))
        if allowed:
            api('editMessageReplyMarkup',dict(chat_id=chat,message_id=message['message_id'],
                reply_markup=dict(inline_keyboard=[])))
    except RuntimeError:
        # Completion is durable even if an old message can no longer be edited.
        pass
    return allowed
