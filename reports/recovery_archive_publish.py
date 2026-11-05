# поток: rev
"""Fixed, owner-approved replies for the explicitly enrolled Yandex review archive.

Does not change live review rules or publish questions. All marketplace writes go through
the existing preflight and natural-key publication ledger. Unknown outcomes are read back.
"""
import html
import os
from pathlib import Path
import time
import uuid

from core import db
from collectors import feedback_send as sender,feedback_preflight as source
from feedback_bot import recovery_runtime as runtime,tg_moderation as bot
from reports import request_class
from reports.feedback_draft_run import review_flagged

ROOT=Path(__file__).resolve().parents[1]
KEYS=['platform','account','kind','ext_id']
POSITIVE='Здравствуйте! Спасибо за отзыв и оценку.'
NEGATIVE='Здравствуйте! Сожалеем, что покупка Вас разочаровала. Если вопрос ещё актуален, напишите нам в чат по QR-коду на упаковке или в товарном чеке.'
APPROVAL='Sergey, 2026-11-04: automatic replies to historical Yandex reviews; negative text explicitly approved in chat'


def setup():
    db.apply_sql_file(ROOT/'migrations/717_rev_archive_publication.sql')


def reply_for(row):
    text=' '.join(filter(None,[row.get('body'),row.get('pros'),row.get('cons')]))
    negative=((row.get('rating') or 0)<=3 or review_flagged(row)
              or request_class.is_claim_text(text,'review',row.get('rating')))
    return ('negative',NEGATIVE) if negative else ('positive',POSITIVE)


def owner_work(row):
    return (row.get('final_text') is not None or row.get('decided_at') is not None
            or row.get('editing') or row.get('moderation_state') in ('skipped','edited','approved_blocked'))


def enroll():
    setup()
    rows=db.query('''SELECT f.*,m.final_text,m.decided_at,m.state AS moderation_state,
        EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id) AS editing
        FROM rev_history_analysis h JOIN raw_feedback f USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE h.platform='yandex' AND h.account='ya_acc1' AND h.kind='review'
        AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false) AND f.posted_at IS NULL''')
    jobs=[]
    for row in rows:
        kind,reply=reply_for(row)
        jobs.append(dict(**{k:row[k] for k in KEYS},reply_kind=kind,approved_text=reply,
                         approval_basis=APPROVAL,state='preserved_owner' if owner_work(row) else 'pending'))
    db.upsert('rev_archive_publication',jobs,KEYS,update_cols=[])
    db.execute('''UPDATE rev_history_analysis h SET state='auto_scheduled',updated_at=now()
        FROM rev_archive_publication a WHERE (a.platform,a.account,a.kind,a.ext_id)=(h.platform,h.account,h.kind,h.ext_id)
        AND a.state='pending' ''')
    # Only unsubmitted, unreserved jobs are removed; paid/inflight results still get collected.
    cancelled=db.execute('''DELETE FROM feedback_llm_queue q USING rev_archive_publication a
        WHERE (q.platform,q.account,q.kind,q.ext_id)=(a.platform,a.account,a.kind,a.ext_id)
        AND a.state='pending' AND q.batch_id IS NULL AND q.submission_key IS NULL''')
    return {'enrolled':len(jobs),'unpaid_llm_jobs_cancelled':cancelled,'states':summary()}


def mark(key,state,detail=None):
    db.execute('''UPDATE rev_archive_publication SET state=%s,detail=%s,updated_at=now()
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',(state,detail,*key))
    history_state={'confirmed':'auto_published','observed_answer':'answered_external',
                   'accepted':'auto_pending_readback','unknown':'auto_pending_readback',
                   'blocked':'auto_blocked','preserved_owner':'preserved',
                   'pending':'auto_scheduled'}[state]
    db.execute('''UPDATE rev_history_analysis SET state=%s,updated_at=now()
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',(history_state,*key))


def update_card(row,state):
    """Reuse the existing card; never send a new moderation message for an archive review."""
    mid=row.get('mod_id')
    if not mid:return True
    m=db.query('SELECT * FROM feedback_moderation WHERE id=%s',(mid,))[0]
    if owner_work(dict(m,moderation_state=m['state'])) and m['state'] not in ('archive_auto','sent','publication_review'):
        return False
    text=row['approved_text']
    if state=='confirmed':
        caption='✅ Отправлено\n\n'+html.escape(text)
        target='sent'
    elif state=='accepted':
        caption='⏳ Ответ принят API. Проверяем появление на Маркете; повторно не отправляем.\n\n'+html.escape(text)
        target='archive_auto'
    elif state=='observed_answer':
        caption='На Маркете уже есть ответ. Архивная карточка закрыта, повтор отключён.'
        target='external_answered'
    else:
        caption='♻️ Архивный отзыв переведён на автоматический ответ по согласованному тексту.\n\n'+html.escape(text)
        target='archive_auto'
    # Rotate revision before the edit; even an old Telegram snapshot cannot execute the old draft.
    db.execute('UPDATE feedback_moderation SET recovery_revision=%s WHERE id=%s',(uuid.uuid4().hex[:10],mid))
    if m.get('tg_chat_id') and m.get('tg_msg_id'):
        response=bot.edit_text(m['tg_chat_id'],m['tg_msg_id'],caption,reply_markup={'inline_keyboard':[]})
        if not response or not response.get('ok'):return False
    kwargs={'error':None}
    if state=='confirmed':kwargs.update(final_text=text,recovery_sent_card_ok=True)
    bot._set(mid,target,**kwargs)
    return True


def withdraw_cards():
    updated=0
    rows=db.query('''SELECT a.*,m.id AS mod_id FROM rev_archive_publication a
        JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE a.state='pending' AND m.state IN ('carded','queued','archived')
        AND m.final_text IS NULL AND m.decided_at IS NULL AND m.tg_msg_id IS NOT NULL''')
    for row in rows:
        with runtime.card_lock(row['mod_id']):
            updated+=bool(update_card(row,'pending'))
    return updated


def refresh_cards():
    rows=db.query('''SELECT a.*,m.id AS mod_id FROM rev_archive_publication a
        JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE (a.state='confirmed' AND (m.state<>'sent' OR NOT m.recovery_sent_card_ok))
           OR (a.state='observed_answer' AND m.state NOT IN ('sent','external_answered'))''')
    updated=0
    for row in rows:
        with runtime.card_lock(row['mod_id']):updated+=bool(update_card(row,row['state']))
    return updated


def reconcile(limit=30):
    confirmed=0
    rows=db.query('''SELECT f.*,a.approved_text,a.state AS archive_state,m.id AS mod_id
        FROM rev_archive_publication a JOIN raw_feedback f USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE a.state IN ('accepted','unknown') ORDER BY a.updated_at LIMIT %s''',(limit,))
    for row in rows:
        key=tuple(row[k] for k in KEYS)
        try:answered,text=source.refresh(row)
        except Exception:continue
        if not answered:continue
        state='confirmed' if (text or '').strip()==row['approved_text'].strip() else 'observed_answer'
        if state=='confirmed':runtime.publication_result(row,'confirmed');confirmed+=1
        mark(key,state)
        with runtime.card_lock(row.get('mod_id')):update_card(row,state)
    return confirmed


def tick(limit=5):
    setup()
    if os.getenv('FEEDBACK_YANDEX_ARCHIVE_AUTOREPLIES','0')!='1':return {'enabled':False}
    if runtime.publication_block():return {'enabled':True,'blocked':runtime.publication_block()}
    result={'enabled':True,'cards_refreshed':refresh_cards(),
            'cards_withdrawn':withdraw_cards(),'confirmed_from_previous':reconcile(),
            'accepted':0,'confirmed':0,'observed_existing':0,'preserved':0,'unknown':0,'cap_reached':False}
    rows=db.query('''SELECT f.*,a.approved_text,a.reply_kind,a.approval_basis,
        m.id AS mod_id,m.final_text,m.decided_at,m.state AS moderation_state,
        EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id) AS editing
        FROM rev_archive_publication a JOIN raw_feedback f USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE a.state='pending' ORDER BY (m.tg_msg_id IS NOT NULL) DESC,f.created_at DESC LIMIT %s''',(limit,))
    for row in rows:
        key=tuple(row[k] for k in KEYS)
        if row['platform']!='yandex' or row['account']!='ya_acc1' or row['kind']!='review':raise RuntimeError('ArchiveScopeViolation')
        if row['approved_text'] not in (POSITIVE,NEGATIVE) or row['approval_basis']!=APPROVAL:raise RuntimeError('UnapprovedArchiveText')
        with runtime.card_lock(row.get('mod_id')):
            # Re-read owner decisions inside the same lock used by callbacks.
            if row.get('mod_id'):
                m=db.query('SELECT * FROM feedback_moderation WHERE id=%s',(row['mod_id'],))[0]
                if owner_work(dict(m,moderation_state=m['state'])):
                    mark(key,'preserved_owner');result['preserved']+=1;continue
            if row.get('is_answered') or row.get('posted_ok'):
                mark(key,'observed_answer');update_card(row,'observed_answer');result['observed_existing']+=1;continue
            if sender.backlog_sent_today('yandex','ya_acc1')>=sender._backlog_cap():
                result['cap_reached']=True;break
            ok,detail=sender.post_answer(row,row['approved_text'],apply_cap=True,override=APPROVAL)
            if ok and detail=='sent':
                mark(key,'accepted');result['accepted']+=1;update_card(row,'accepted')
                try:answered,text=source.refresh(row)
                except Exception:answered,text=False,''
                if answered and (text or '').strip()==row['approved_text'].strip():
                    mark(key,'confirmed');runtime.publication_result(row,'confirmed')
                    update_card(row,'confirmed');result['confirmed']+=1
                else:
                    runtime.publication_result(row,'readback_missing')
            elif ok and detail.startswith('dry-run'):
                result['cap_reached']='cap' in detail
                break
            else:
                current=db.query('SELECT is_answered,posted_ok FROM raw_feedback WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)',key)[0]
                attempt=db.query('SELECT state FROM rev_publication_attempt WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)',key)
                if current['is_answered'] or current['posted_ok']:
                    mark(key,'observed_answer');update_card(row,'observed_answer');result['observed_existing']+=1
                elif attempt and attempt[0]['state'] in ('inflight','unknown','readback_missing'):
                    mark(key,'unknown',detail);update_card(row,'accepted');result['unknown']+=1
                elif attempt and attempt[0]['state']=='rejected':
                    mark(key,'blocked',detail)
                    break
                else:
                    mark(key,'pending',detail)
                    break  # A read/API error is not a reason to hammer the next records.
        time.sleep(3)
    result['states']=summary()
    return result


def summary():
    return db.query('''SELECT state,reply_kind,count(*) AS n FROM rev_archive_publication
        GROUP BY 1,2 ORDER BY 1,2''')
