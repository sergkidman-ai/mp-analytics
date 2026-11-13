# поток: rev
"""Persistent provider failures and Telegram alert outbox; no raw API error storage."""
import datetime
import html
from pathlib import Path
from psycopg2.extras import RealDictCursor
from core import db

_READY = False


def setup():
    global _READY
    if not _READY:
        db.apply_sql_file(Path(__file__).resolve().parents[1]/'migrations/718_rev_provider_alerts.sql')
        _READY = True


def label(provider):
    return {'anthropic':'Anthropic', 'deepseek':'DeepSeek', 'deepseek-balance':'DeepSeek',
            'pipeline':'Очередь ИИ'}.get(provider, provider)


def classify(error, provider='anthropic'):
    code = getattr(error, 'status_code', None)
    message = str(getattr(error, 'body', None) or '').lower()
    name = label(provider)
    if ('workspace api usage limits' in message or 'spend limit' in message
            or ('usage limit' in message and ('workspace' in message or 'organization' in message))
            or (code == 429 and ('monthly' in message or 'spend cap' in message))):
        return 'workspace_limit', name+': достигнут лимит расходов workspace/организации. Проверьте лимит в консоли провайдера.'
    if code == 402 or 'credit balance is too low' in message or 'insufficient balance' in message:
        return 'billing', name+': закончились кредиты или есть проблема оплаты. Проверьте баланс и оплату в консоли провайдера.'
    if code in (401, 403):
        return 'authorization', name+': отказ авторизации/доступа; проверьте ключ и его разрешения.'
    if code == 429:
        return 'rate_limit', name+': временный лимит частоты запросов; повторим автоматически.'
    if code in (400, 404, 413, 422):
        return 'request_rejected', name+': запрос отклонён; требуется проверка конфигурации.'
    return 'unknown_submission', name+': нет подтверждённого результата API; повторим сверку. Это не подтверждённая ошибка баланса.'


def rejected(error):
    return getattr(error, 'status_code', None) in (400, 401, 402, 403, 404, 413, 422, 429)


def _enqueue(cur, provider, kind, reason, severity, message):
    cur.execute('''INSERT INTO rev_provider_alert(provider,kind,reason,severity,message)
        VALUES(%s,%s,%s,%s,%s)''', (provider,kind,reason,severity,message))


def observe(provider, reason, detail, *, severity='critical', blocks_work=True,
            source='request', transient=False, retry_seconds=300, repeat_seconds=21600):
    setup()
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('rev_provider:'+provider,))
            cur.execute('SELECT * FROM rev_batch_health WHERE provider=%s FOR UPDATE', (provider,))
            previous = cur.fetchone()
            failures = previous['failures']+1 if previous and previous['reason']==reason else 1
            if transient:
                severity = 'warning' if failures==1 else 'critical'
            due = (not previous or previous['reason']!=reason or previous['severity']!=severity
                   or not previous['last_event_at']
                   or (now-previous['last_event_at']).total_seconds()>=repeat_seconds)
            event_at = now if due else previous['last_event_at']
            cur.execute('''INSERT INTO rev_batch_health
                (provider,reason,detail,severity,source,blocks_work,failures,last_event_at,retry_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second')
                ON CONFLICT(provider) DO UPDATE SET reason=EXCLUDED.reason,detail=EXCLUDED.detail,
                    severity=EXCLUDED.severity,source=EXCLUDED.source,blocks_work=EXCLUDED.blocks_work,
                    failures=EXCLUDED.failures,last_event_at=EXCLUDED.last_event_at,
                    retry_at=EXCLUDED.retry_at,updated_at=now()''',
                (provider,reason,detail,severity,source,blocks_work,failures,event_at,retry_seconds))
            if due:
                impact = ('Зависящая от провайдера обработка приостановлена. Сбор обращений и простые '
                          'автоответы продолжаются. Повторная проверка — через 5 минут.' if blocks_work
                          else 'Работа продолжается. Предупреждение заранее.')
                icon = '🔴' if severity=='critical' else '🟡'
                message = icon+' Rev · '+html.escape(label(provider))+'\n'+html.escape(detail)+'\n\n'+impact
                _enqueue(cur,provider,'problem',reason,severity,message)
    deliver_alerts(provider=provider)
    return detail


def record(error, provider='anthropic', source='request'):
    reason, detail = classify(error,provider)
    return observe(provider,reason,detail,source=source,
                   transient=reason in ('rate_limit','unknown_submission'))


def blocked(provider='anthropic'):
    setup()
    rows = db.query('''SELECT detail FROM rev_batch_health
        WHERE provider=%s AND blocks_work AND retry_at>now()''', (provider,))
    return rows[0]['detail'] if rows else None


def clear(provider='anthropic', source=None, notify_recovery=True):
    """Only a successful operation resolves its failure; balance checks are scoped."""
    setup()
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('rev_provider:'+provider,))
            cur.execute('SELECT * FROM rev_batch_health WHERE provider=%s FOR UPDATE', (provider,))
            previous = cur.fetchone()
            if not previous or (source and previous['source']!=source and
                                not (source=='balance' and previous['reason']=='billing')):
                return False
            cur.execute('''UPDATE rev_provider_alert SET superseded_at=now()
                WHERE provider=%s AND delivered_at IS NULL AND superseded_at IS NULL''', (provider,))
            if notify_recovery:
                resolved = ('Доступ восстановлен. Обработка возобновляется автоматически.'
                            if previous['blocks_work'] else 'Проверка успешна. Предупреждение снято.')
                message = '🟢 Rev · '+html.escape(label(provider))+'\n'+resolved
                _enqueue(cur,provider,'recovery',previous['reason'],'healthy',message)
            cur.execute('DELETE FROM rev_batch_health WHERE provider=%s', (provider,))
    if notify_recovery:
        deliver_alerts(provider=provider)
    return True


def _send(message):
    from feedback_bot import tg_moderation as bot
    response = bot.api('sendMessage', {'chat_id':bot.NOTIFY_IDS[0], 'text':message,
                                      'parse_mode':'HTML','disable_web_page_preview':True}, timeout=8)
    return (response.get('result') or {}).get('message_id') if response.get('ok') else None


def deliver_alerts(provider=None, limit=10):
    """Independent timer retries Telegram delivery; duplicate workers share a lock."""
    setup()
    delivered = 0
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('rev_provider_alert_delivery')) AS acquired")
            if not cur.fetchone()['acquired']:
                return 0
            cur.execute('''SELECT * FROM rev_provider_alert WHERE delivered_at IS NULL
                AND superseded_at IS NULL AND (%s IS NULL OR provider=%s) ORDER BY id LIMIT %s''',
                (provider,provider,limit))
            for row in cur.fetchall():
                try:
                    message_id = _send(row['message'])
                    error = None if message_id else 'TelegramNotConfirmed'
                except Exception as exc:
                    message_id,error = None,type(exc).__name__
                if message_id:
                    cur.execute('''UPDATE rev_provider_alert SET delivered_at=now(),message_id=%s,
                        last_error=NULL WHERE id=%s''', (message_id,row['id']))
                    delivered += 1
                else:
                    cur.execute('UPDATE rev_provider_alert SET last_error=%s WHERE id=%s', (error,row['id']))
                    break
    return delivered


def status():
    setup()
    rows = db.query("SELECT reason,detail,updated_at,retry_at FROM rev_batch_health WHERE provider='anthropic'")
    queue = db.query('''SELECT count(*) FILTER(WHERE batch_id IS NULL) AS waiting_requests,
        count(*) FILTER(WHERE batch_id IS NULL AND submission_key IS NOT NULL) AS uncertain_requests,
        min(created_at) AS oldest_request FROM feedback_llm_queue''')[0]
    queue['submitted_batches'] = db.query("SELECT count(*) AS n FROM feedback_llm_batch WHERE status='submitted'")[0]['n']
    providers=db.query('SELECT provider,reason,detail,severity,blocks_work FROM rev_batch_health ORDER BY provider')
    return dict(queue, provider=rows[0] if rows else None, providers=providers)
