# поток: rev
"""Bounded recovery for four operator-approved Ozon questions from 12-13 Nov.

Runs only after HTTPS is reachable and a fresh per-question readback succeeds.
Unknown outcomes stay blocked, except the documented four explicit HTTP 429
rejections that the old sender mistakenly recorded as unknown.
"""
import json
import socket
from pathlib import Path

from core import db
from collectors import feedback_preflight
from feedback_bot import recovery_runtime as runtime, tg_moderation as bot

IDS=(47716,17938,63196,15062)
OUT=Path('/root/rev-recovery/ozon-approved-retry-latest.json')


def reachable():
    try:
        with socket.create_connection(('api-seller.ozon.ru',443),timeout=3):
            return True
    except OSError:
        return False


def old_429_rejected(row):
    """Correct only the known false 'unknown' classification, after empty readback."""
    if row['id']!=47716 or not (row.get('error') or '').startswith('RuntimeError: Ozon question/answer: исчерпаны ретраи по 429'):
        return False
    key=tuple(row[name] for name in ('platform','account','kind','ext_id'))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT state,final_text FROM rev_publication_attempt
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s) FOR UPDATE''',key)
            attempt=cur.fetchone()
            cur.execute('''SELECT attempts,blocked,last_error FROM feedback_send_attempts
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s) FOR UPDATE''',key)
            failure=cur.fetchone()
            if (not attempt or attempt!=("unknown",row['final_text']) or not failure
                    or failure[0]!=1 or not failure[1]
                    or not failure[2].startswith('RuntimeError: Ozon question/answer: исчерпаны ретраи по 429')):
                return False
            cur.execute('''UPDATE rev_publication_attempt SET state='rejected',updated_at=now()
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',key)
            cur.execute('''UPDATE feedback_send_attempts SET blocked=false
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',key)
    return True


def run():
    pending=db.query('''SELECT count(*) AS n FROM feedback_moderation
        WHERE id=ANY(%s) AND state='failed' AND final_text IS NOT NULL''',(list(IDS),))[0]['n']
    if not pending:return {'pending':0,'retried':0}
    if not reachable():return {'network':'unreachable','retried':0}
    outcomes=[]
    for mid in IDS:
        with runtime.card_lock(mid):
            rows=db.query('''SELECT m.id,m.state,m.final_text,m.decided_at,m.decided_by,m.error,
                m.tg_chat_id,m.tg_msg_id,f.platform,f.account,f.kind,f.ext_id,f.is_answered,
                f.posted_ok,f.recovery_active_unanswered FROM feedback_moderation m
                JOIN raw_feedback f USING(platform,account,kind,ext_id) WHERE m.id=%s''',(mid,))
            if not rows:continue
            row=rows[0]
            if (row['state']!='failed' or not row['final_text'] or not row['decided_at']
                    or row['decided_by'] is None or row['is_answered'] or row['posted_ok']
                    or row['recovery_active_unanswered'] is False):continue
            attempts=db.query('''SELECT blocked FROM feedback_send_attempts
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',
                tuple(row[name] for name in ('platform','account','kind','ext_id')))
            if attempts and attempts[0]['blocked'] and not (
                    mid==47716 and (row.get('error') or '').startswith(
                        'RuntimeError: Ozon question/answer: исчерпаны ретраи по 429')):
                outcomes.append({'id':mid,'result':'attempts_exhausted'})
                continue
            try:
                answered,text=feedback_preflight.refresh(row)
            except Exception as error:
                outcomes.append({'id':mid,'result':'read_failed','type':type(error).__name__})
                continue
            if answered:
                if mid==47716 and text.strip()==row['final_text'].strip():
                    from collectors import feedback_send
                    feedback_send._mark_posted(row,text,True)
                    runtime.publication_result(row,'confirmed')
                    bot._set(mid,'sent',error=None,recovery_sent_card_ok=True)
                    bot.edit_text(row['tg_chat_id'],row['tg_msg_id'],'✅ Отправлено. Ответ подтверждён площадкой.')
                    outcomes.append({'id':mid,'result':'confirmed_existing'})
                else:
                    bot._set(mid,'external_answered',error=None)
                    bot.edit_text(row['tg_chat_id'],row['tg_msg_id'],'✅ На площадке уже есть ответ. Повторная отправка отключена.')
                    outcomes.append({'id':mid,'result':'already_answered'})
                continue
            if mid==47716 and not old_429_rejected(row):
                outcomes.append({'id':mid,'result':'unknown_retained'})
                continue
            result=bot._do_send(mid,str(row['decided_by']),row['final_text'],
                                row['tg_chat_id'],row['tg_msg_id'],override_block=True)
            outcomes.append({'id':mid,'result':'sent' if result=='✅ Отправлено' else 'not_sent'})
    summary={'network':'reachable','retried':len(outcomes),'outcomes':outcomes}
    OUT.write_text(json.dumps(summary));OUT.chmod(0o600)
    return summary


if __name__=='__main__':
    import fcntl
    OUT.parent.mkdir(parents=True,exist_ok=True)
    with open('/root/rev-recovery/ozon-approved-retry.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        print(json.dumps(run()))
