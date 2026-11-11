# поток: card
"""Счётчик и намерение фиксируются ДО запроса записи. Неизвестный результат не повторяем."""
from psycopg2.extras import Json
from core import db


def control():
    return db.query('SELECT * FROM card_recovery_control WHERE singleton')[0]


def pool(account, limit, classes, offers=None, preview=False):
    return db.query("""WITH eligible AS (
        SELECT s.offer_id,s.product_id,s.attempts,s.is_selling,s.err_class,s.err_codes,
               s.history_missing,CASE WHEN s.history_missing THEN 1 ELSE 3 END attempt_limit,
               row_number() OVER(PARTITION BY s.history_missing ORDER BY s.err_class,s.is_selling,s.first_seen,s.offer_id) AS priority_rank
        FROM card_status s CROSS JOIN card_recovery_control r
        WHERE s.platform='ozon' AND s.account=%s AND s.is_open AND s.err_class=ANY(%s)
          AND (%s IS NULL OR s.offer_id=ANY(%s)) AND NOT s.needs_human
          AND r.paused_reason IS NULL
          AND (NOT s.history_missing OR r.legacy_once_enabled OR %s)
          AND s.attempts < CASE WHEN s.history_missing THEN 1 ELSE 3 END
          AND (s.last_attempt_at IS NULL OR s.last_attempt_at<now()-interval '20 hours')
          AND NOT EXISTS(SELECT 1 FROM card_push_log l WHERE l.platform='ozon'
              AND l.account=s.account AND l.offer_id=s.offer_id
              AND (l.task_status IN ('prepared','pending','uncertain')
                   OR (l.task_id IS NOT NULL AND l.healed IS NULL)))
        ) SELECT * FROM eligible WHERE NOT history_missing OR priority_rank <= greatest(0,
          (SELECT legacy_daily_limit FROM card_recovery_control WHERE singleton)-
          (SELECT count(*) FROM card_push_log WHERE platform='ozon' AND account=%s
           AND recovery_attempt AND ts >= date_trunc('day',now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow'))
        ORDER BY err_class,is_selling,priority_rank,offer_id LIMIT %s""",
        (account,list(classes),offers,offers,preview,account,limit))


def reserve(account, card, baseline):
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(600604)')
        cur.execute('SELECT legacy_once_enabled,legacy_daily_limit,paused_reason FROM card_recovery_control WHERE singleton')
        allowed, daily_limit, paused = cur.fetchone()
        cur.execute("""SELECT attempts,needs_human,history_missing,is_open,
            (last_attempt_at IS NULL OR last_attempt_at<now()-interval '20 hours'),err_class,product_id
            FROM card_status
            WHERE platform='ozon' AND account=%s AND offer_id=%s FOR UPDATE""",(account,card['offer_id']))
        row=cur.fetchone()
        if not row:
            return None
        attempts,human,missing,opened,backoff,err_class,product_id=row
        if (paused or human or not opened or not backoff or err_class!=card['err_class']
                or product_id!=card['product_id'] or (missing and not allowed)
                or attempts >= (1 if missing else 3)):
            return None
        cur.execute("""SELECT 1 FROM card_push_log WHERE platform='ozon' AND account=%s AND offer_id=%s
            AND (task_status IN ('prepared','pending','uncertain')
                 OR (task_id IS NOT NULL AND healed IS NULL)) LIMIT 1""",(account,card['offer_id']))
        if cur.fetchone():
            return None
        if missing:
            cur.execute("""SELECT count(*) FROM card_push_log WHERE platform='ozon' AND account=%s
                AND recovery_attempt AND ts>=date_trunc('day',now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow'""",(account,))
            if cur.fetchone()[0] >= daily_limit:
                return None
        cur.execute("""INSERT INTO card_push_log(platform,account,offer_id,product_id,rung,
            task_status,action_state,baseline,recovery_attempt,note)
            VALUES('ozon',%s,%s,%s,1,'prepared','prepared',%s,%s,%s) RETURNING id""",
            (account,card['offer_id'],card['product_id'],Json(baseline),missing,'class:'+card['err_class']))
        identity=cur.fetchone()[0]
        cur.execute("""UPDATE card_status SET attempts=attempts+1,last_attempt_at=now(),
            needs_human=(attempts+1 >= %s) WHERE platform='ozon' AND account=%s AND offer_id=%s""",
            (1 if missing else 3,account,card['offer_id']))
        return identity


def save_payload(identity, body):
    count=db.execute("UPDATE card_push_log SET request_payload=%s WHERE id=%s AND task_status='prepared'",
                     (Json(body),identity))
    if count!=1:
        raise RuntimeError('Card update intent not durable')


def finish(identity, record):
    return db.execute("""UPDATE card_push_log SET attr_id=%s,http_code=%s,task_id=%s,
        task_status=%s,action_state=%s,note=note || ' ' || %s WHERE id=%s""",
        (record.get('attr_id'),record.get('http_code'),record.get('task_id'),record.get('task_status'),
         'submitted' if record.get('task_id') else 'no_confirmation',record.get('note') or '',identity))


def uncertain(identity, error):
    db.execute("""UPDATE card_push_log SET task_status='uncertain',action_state='uncertain',
        error_kind=%s WHERE id=%s""",(type(error).__name__,identity))


def pause(reason):
    db.execute('UPDATE card_recovery_control SET paused_reason=%s WHERE singleton',(reason,))
