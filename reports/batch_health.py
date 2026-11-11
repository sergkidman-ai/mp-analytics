# поток: rev
"""Safe, persistent Batch provider failures; no keys, URLs or raw error bodies."""
from core import db


def setup():
    db.execute('''CREATE TABLE IF NOT EXISTS rev_batch_health (
        provider text PRIMARY KEY, reason text NOT NULL, detail text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(), retry_at timestamptz NOT NULL)''')


def classify(error):
    code = getattr(error, 'status_code', None)
    body = getattr(error, 'body', None)
    message = str(body or '').lower()
    if code == 400 and 'workspace api usage limits' in message:
        return 'workspace_limit', 'Anthropic: достигнут лимит расходов workspace. Увеличьте лимит в Anthropic Console.'
    if code in (401, 403):
        return 'authorization', 'Anthropic: отказ авторизации; проверьте доступ ключа.'
    if code == 429:
        return 'rate_limit', 'Anthropic: временный лимит частоты запросов.'
    if code in (400, 404, 413, 422):
        return 'request_rejected', 'Anthropic: запрос Batch отклонён; требуется проверка конфигурации.'
    return 'unknown_submission', 'Anthropic: результат создания Batch неизвестен; повтор до сверки запрещён.'


def rejected(error):
    return getattr(error, 'status_code', None) in (400, 401, 403, 404, 413, 422, 429)


def record(error):
    setup()
    reason, detail = classify(error)
    seconds = 21600 if reason in ('workspace_limit', 'authorization', 'request_rejected') else 900
    db.execute('''INSERT INTO rev_batch_health(provider,reason,detail,retry_at)
        VALUES('anthropic',%s,%s,now()+%s*interval '1 second') ON CONFLICT(provider)
        DO UPDATE SET reason=EXCLUDED.reason,detail=EXCLUDED.detail,
            updated_at=now(),retry_at=EXCLUDED.retry_at''', (reason, detail, seconds))
    return detail


def blocked():
    setup()
    rows = db.query("SELECT detail FROM rev_batch_health WHERE provider='anthropic' AND retry_at>now()")
    return rows[0]['detail'] if rows else None


def clear():
    db.execute("DELETE FROM rev_batch_health WHERE provider='anthropic'")


def status():
    setup()
    rows = db.query("SELECT reason,detail,updated_at,retry_at FROM rev_batch_health WHERE provider='anthropic'")
    queue = db.query('''SELECT count(*) FILTER(WHERE batch_id IS NULL) AS waiting_requests,
        count(*) FILTER(WHERE batch_id IS NULL AND submission_key IS NOT NULL) AS uncertain_requests,
        min(created_at) AS oldest_request FROM feedback_llm_queue''')[0]
    queue['submitted_batches'] = db.query("SELECT count(*) AS n FROM feedback_llm_batch WHERE status='submitted'")[0]['n']
    return dict(queue, provider=rows[0] if rows else None)
