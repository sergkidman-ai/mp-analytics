# поток: rev
"""Authoritative read immediately before publication or unknown-outcome reconciliation."""
import os
import html
import uuid
import requests
from core import db


def probe(row):
    platform, account, kind, identifier = (row[k] for k in ['platform','account','kind','ext_id'])
    if platform == 'wb':
        from collectors.wb_feedbacks import BASE, feedback_token
        response = requests.get(BASE+('/question' if kind=='question' else '/feedback'),
            headers={'Authorization':feedback_token(account)},params={'id':identifier},timeout=(8,25))
        response.raise_for_status()
        result = response.json()
        if result.get('error') or not isinstance(result.get('data'),dict):
            raise RuntimeError('InvalidWBReadback')
        data = result['data']
        if str(data.get('id')) != str(identifier):
            raise RuntimeError('MismatchedWBReadback')
        answer = data.get('answer') or {}
        text = answer.get('text') if isinstance(answer,dict) else answer
        return bool(text), text or ''
    if platform == 'ozon' and kind == 'question':
        from collectors.ozon_feedbacks import _headers
        response = requests.post('https://api-seller.ozon.ru/v1/question/info',
            headers=_headers(account),json={'question_id':identifier},timeout=(8,25))
        response.raise_for_status()
        data = response.json()
        if str(data.get('id')) != str(identifier) or not isinstance(data.get('answers_count'),int):
            raise RuntimeError('InvalidOzonReadback')
        from collectors.ozon_answer_owner import own_reply
        if data['answers_count'] == 0:
            return False,''
        mine,_=own_reply(account,identifier,data.get('sku') or row.get('item_id'))
        return bool(mine),(mine or {}).get('text') or ''
    if platform == 'yandex' and kind == 'review':
        from collectors.feedback_recovery import yandex_review_answer
        mine, _ = yandex_review_answer(os.environ['YANDEX_API_KEY_ACC1'],
            os.environ['YANDEX_BUSINESS_ID_ACC1'],identifier,expected_count=None)
        return bool(mine), (mine or {}).get('text') or ''
    if platform == 'yandex' and kind == 'question':
        from collectors import yandex_questions as yq
        key, business = yq._cfg()
        mine = yq._our_answer(yq.answers(key,business,identifier),yq._shop_names())
        return bool(mine), (mine or {}).get('text') or ''
    raise RuntimeError('NoSafeReadbackForChannel')


def refresh(row):
    answered, text = probe(row)
    db.execute('''UPDATE raw_feedback SET recovery_status_checked_at=now(),
        is_answered=(is_answered OR %s),answer_text=CASE WHEN %s<>'' THEN %s ELSE answer_text END
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',
        (answered,text,text,*(row[k] for k in ['platform','account','kind','ext_id'])))
    return answered,text


def reconcile_unknown():
    from collectors import feedback_send
    from feedback_bot import recovery_runtime
    summary = {'confirmed':0,'observed_existing':0,'still_unknown':0,'read_errors':0}
    for row in db.query('''SELECT f.*,a.final_text AS attempted_text
        FROM rev_publication_attempt a JOIN raw_feedback f USING(platform,account,kind,ext_id)
        WHERE a.state IN ('unknown','inflight','readback_missing') AND a.created_at<now()-interval '2 minutes' '''):
        try:
            answered, text = refresh(row)
        except Exception:
            summary['read_errors']+=1
            continue
        if not answered:
            summary['still_unknown']+=1
            continue  # Visibility can lag; a negative read alone does not authorize resubmission.
        if text.strip() and text.strip()==row['attempted_text'].strip():
            feedback_send._mark_posted(row,text,True)
            recovery_runtime.publication_result(row,'confirmed')
            summary['confirmed']+=1
        else:
            recovery_runtime.publication_result(row,'observed_answer')
            summary['observed_existing']+=1
    return summary


def mark_missing(row):
    from feedback_bot import recovery_runtime
    recovery_runtime.publication_result(row,'readback_missing')
    db.execute('''UPDATE feedback_moderation SET state='publication_review',
        recovery_revision=%s,recovery_sent_card_ok=false,
        error='API accepted the answer, but subsequent readback did not return it; no resend'
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s) AND state='sent' ''',
        (uuid.uuid4().hex[:10],*(row[k] for k in ['platform','account','kind','ext_id'])))


def verify_recent_publications(limit=20):
    result={'present':0,'missing':0,'read_errors':0}
    for row in db.query('''SELECT f.* FROM raw_feedback f JOIN rev_publication_attempt a
        USING(platform,account,kind,ext_id) WHERE a.state='confirmed'
        AND f.posted_ok AND f.posted_at>now()-interval '2 days'
        AND a.updated_at<now()-interval '6 hours'
        ORDER BY a.updated_at LIMIT %s''',(limit,)):
        try:
            answered,_=probe(row)
        except Exception:
            result['read_errors']+=1
            db.execute('''UPDATE rev_publication_attempt SET updated_at=now()
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s) AND state='confirmed' ''',
                tuple(row[k] for k in ['platform','account','kind','ext_id']))
            continue
        if answered:
            result['present']+=1
            db.execute('''UPDATE rev_publication_attempt SET updated_at=now()
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s) AND state='confirmed' ''',
                tuple(row[k] for k in ['platform','account','kind','ext_id']))
        else:
            mark_missing(row)
            result['missing']+=1
    return result


def notify_readback_reviews():
    from feedback_bot import recovery_runtime,tg_moderation as bot
    updated=0
    for candidate in db.query("SELECT id FROM feedback_moderation WHERE state='publication_review' AND NOT recovery_sent_card_ok"):
        with recovery_runtime.card_lock(candidate['id']):
            rows=db.query('''SELECT m.*,f.product_name,f.body,
                coalesce(m.final_text,f.answer_text,f.draft_text) AS sent_text
                FROM feedback_moderation m JOIN raw_feedback f USING(platform,account,kind,ext_id)
                WHERE m.id=%s AND m.state='publication_review' AND NOT m.recovery_sent_card_ok''',(candidate['id'],))
            if not rows:
                continue
            row=rows[0]
            response=bot.edit_text(row['tg_chat_id'],row['tg_msg_id'],
                '🟡 <b>Ответ принят API, но сейчас не возвращается при сверке площадки.</b>\n'
                +html.escape(row['platform']+' · '+(row.get('product_name') or '')[:160])+'\n'
                +html.escape((row.get('body') or '')[:250])+'\n\n'
                +
                'Повторно не отправляли. Причина пока неизвестна; проверьте ответ в кабинете площадки.\n\n'
                '<b>Ранее отправленный текст:</b>\n'+html.escape((row.get('sent_text') or '')[:1500]))
            if response and response.get('ok'):
                db.execute('UPDATE feedback_moderation SET recovery_sent_card_ok=true WHERE id=%s',(row['id'],))
                updated+=1
    return updated
