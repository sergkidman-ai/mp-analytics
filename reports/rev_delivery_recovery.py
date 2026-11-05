# поток: rev
"""Inspect six delivery tails and perform at most one bounded retry of a proven 404 rejection.

An empty readback never authorizes retrying a timeout/unknown outcome. Existing ledger rows
are retained and claimed by compare-and-set, and the normal daily cap is enforced.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import time

import requests
from core import db
from collectors import feedback_send as sender
from collectors.yandex_questions import _cfg
from feedback_bot import recovery_runtime as runtime
from reports import recovery_archive_publish as archive

CASE_IDS=('187406026','215882362','219745079','280307265','317972078','411084363')
KNOWN_TIMEOUT='215882362'
PUBLISHED={'PUBLISHED','UNMODERATED'}
ROOT=Path('/root/rev-recovery/task-delivery-20261105')
DOCS='https://yandex.ru/dev/market/partner-api/doc/ru/concepts/error-codes'


def key(row):
    value=tuple(row[field] for field in ('platform','account','kind','ext_id'))
    if value[:3]!=('yandex','ya_acc1','review') or value[3] not in CASE_IDS:
        raise ValueError('Delivery recovery scope violation')
    return value


def cases():
    return db.query('''SELECT a.*,f.payload,f.posted_at,f.is_answered,f.posted_ok,
        l.state AS ledger_state,l.final_text AS attempted_text,l.created_at AS attempt_started_at,
        coalesce(s.attempts,0) AS attempts,coalesce(s.blocked,false) AS attempt_blocked,
        m.id AS mod_id,m.final_text,m.decided_at,m.state AS moderation_state,
        EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id) AS editing
        FROM rev_archive_publication a JOIN raw_feedback f USING(platform,account,kind,ext_id)
        JOIN rev_publication_attempt l USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_send_attempts s USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE a.ext_id=ANY(%s) AND a.platform='yandex' AND a.account='ya_acc1' AND a.kind='review'
        ORDER BY a.ext_id''',(list(CASE_IDS),))


def read_api(endpoint,body,params=None):
    token,business=_cfg()
    response=requests.post('https://api.partner.market.yandex.ru/v2/businesses/'+business+endpoint,
        headers={'Api-Key':token},json=body,params=params or {'limit':20},timeout=(8,30))
    trace={name:value for name,value in response.headers.items()
           if any(word in name.lower() for word in ('request-id','req-id','trace-id')) or name.lower().endswith('-rid')}
    try:data=response.json()
    except ValueError:data={'unparseable_json':True}
    return {'http_status':response.status_code,'request_ids':trace,'data':data}


def inspect(row,read=read_api):
    key(row);identifier=int(row['ext_id'])
    feedback=read('/goods-feedback',{'feedbackIds':[identifier]})
    comments=[];pages=[];cursor=None;seen=set();complete=False
    for _ in range(50):
        params={'limit':20}
        if cursor:params['pageToken']=cursor
        page=read('/goods-feedback/comments',{'feedbackId':identifier},params)
        pages.append(page)
        if page['http_status']!=200:break
        result=page.get('data',{}).get('result') or {}
        comments.extend(result.get('comments') or [])
        cursor=(result.get('paging') or {}).get('nextPageToken')
        if not cursor:complete=True;break
        if cursor in seen:break
        seen.add(cursor)
    evidence={'id':row['ext_id'],'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
              'feedback':feedback,'comment_pages':pages,'comments':comments,'comments_complete':complete}
    evidence['classification']=classify(row,evidence)
    return evidence


def classify(row,evidence):
    identifier=row['ext_id'];feedback=evidence.get('feedback') or {}
    if (feedback.get('http_status')!=200 or feedback.get('data',{}).get('status')!='OK'
            or not evidence.get('comments_complete')
            or any(page.get('data',{}).get('status')!='OK' for page in evidence.get('comment_pages',[]))):
        return 'read_incomplete'
    items=(feedback.get('data',{}).get('result') or {}).get('feedbacks') or []
    if not any(str(item.get('feedbackId'))==identifier for item in items):return 'not_returned_by_read_api'
    own=[comment for comment in evidence.get('comments',[]) if comment.get('canModify') is True]
    visible=[comment for comment in own if comment.get('status') in PUBLISHED]
    if any((comment.get('text') or '').strip()==row['approved_text'].strip() for comment in visible):
        return 'matching_own_answer'
    if visible:return 'other_own_answer'
    if own:return 'own_comment_requires_moderation_review'
    if identifier==KNOWN_TIMEOUT or row.get('ledger_state') in ('unknown','inflight','readback_missing'):
        return 'unknown_outcome_empty_readback'
    return 'readable_but_write_rejected_404' if is_known_404(row) else 'no_own_answer'


def is_known_404(row):
    return (row.get('ledger_state')=='rejected' and row.get('state')=='blocked'
            and re.search(r'\b404\b',row.get('detail') or '') is not None
            and 'feedbacks not found' in (row.get('detail') or '').lower())


def retry_allowed(row,evidence,used,cap):
    key(row)
    if row['ext_id']==KNOWN_TIMEOUT or row.get('ledger_state') in ('unknown','inflight','readback_missing'):
        return False,'unknown_outcome_never_retry'
    if not is_known_404(row):return False,'not_a_proven_404_rejection'
    if row.get('attempts')!=1 or row.get('attempt_blocked'):return False,'bounded_retry_exhausted'
    if row['approved_text'] not in (archive.POSITIVE,archive.NEGATIVE) or row.get('approval_basis')!=archive.APPROVAL:
        return False,'unapproved_text'
    if archive.owner_work(row):return False,'operator_decision_preserved'
    if used>=cap:return False,'daily_cap_reached'
    if evidence.get('classification')!='readable_but_write_rejected_404':return False,'fresh_readback_does_not_authorize_retry'
    try:age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(evidence['at_utc'])).total_seconds()
    except (KeyError,ValueError,TypeError):return False,'readback_timestamp_invalid'
    if not 0<=age<=120:return False,'readback_expired'
    return True,'one_confirmed_rejection_retry_allowed'


def save_evidence(evidence):
    ROOT.mkdir(parents=True,exist_ok=True)
    path=ROOT/(evidence['id']+'-latest.json')
    path.write_text(json.dumps(evidence,ensure_ascii=False,default=str,indent=2))
    path.chmod(0o600)


def retry_known_rejections(limit=1,read=read_api):
    """One additional attempt per known rejection, only when the existing quota permits it."""
    if limit!=1:raise ValueError('This recovery operation permits exactly one bounded attempt')
    outcomes=[]
    with runtime.processing_lock():
        if runtime.publication_block() or not sender._live() or 'ya_acc1' not in sender._live_accounts():
            return [{'result':'publishing_disabled'}]
        used=sender.backlog_sent_today('yandex','ya_acc1');cap=sender._backlog_cap()
        if used>=cap:return [{'result':'daily_cap_reached','used':used,'cap':cap,'writes':0}]
        for row in cases():
            if not is_known_404(row) or row.get('attempts')!=1 or row.get('attempt_blocked'):continue
            evidence=inspect(row,read);save_evidence(evidence)
            allowed,reason=retry_allowed(row,evidence,used,cap)
            if not allowed:
                outcomes.append({'id':row['ext_id'],'result':reason,'writes':0});continue
            with runtime.card_lock(row.get('mod_id')):
                # Read operator edits/decisions again while holding the same lock as the bot.
                current=next((item for item in cases() if key(item)==key(row)),None)
                if current is None:
                    outcomes.append({'id':row['ext_id'],'result':'case_changed','writes':0});continue
                allowed,reason=retry_allowed(current,evidence,sender.backlog_sent_today('yandex','ya_acc1'),cap)
                if not allowed:
                    outcomes.append({'id':row['ext_id'],'result':reason,'writes':0});continue
                row=current
                # The original row and attempt start time are preserved. No DELETE/reinsert.
                claimed=db.execute('''UPDATE rev_publication_attempt SET state='inflight',updated_at=now()
                    WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)
                    AND state='rejected' AND final_text=%s''',(*key(row),row['approved_text']))
                if not claimed:
                    outcomes.append({'id':row['ext_id'],'result':'ledger_changed','writes':0});continue
                try:sender.send_yandex_review(row['account'],row['ext_id'],row['approved_text'])
                except Exception as error:
                    status=error.response.status_code if isinstance(error,requests.HTTPError) and error.response is not None else None
                    definitive=status in (400,401,403,404,405,422)
                    state='rejected' if definitive else 'unknown'
                    runtime.publication_result(row,state)
                    sender._bump_attempt(row,'bounded recovery retry: '+(str(status) if status else type(error).__name__),terminal=not definitive)
                    archive.mark(key(row),'blocked' if definitive else 'unknown','bounded_retry_'+state)
                    outcomes.append({'id':row['ext_id'],'result':state,'http_status':status,'writes':1})
                else:
                    sender._mark_posted(row,row['approved_text'],True)
                    runtime.publication_result(row,'readback_missing')
                    archive.mark(key(row),'accepted')
                    after=inspect(dict(row,state='accepted',ledger_state='readback_missing'),read)
                    save_evidence(after)
                    if after['classification']=='matching_own_answer':
                        runtime.publication_result(row,'confirmed');archive.mark(key(row),'confirmed')
                        archive.update_card(row,'confirmed')
                        result='confirmed'
                    else:result='accepted_waiting_readback'
                    used+=1
                    outcomes.append({'id':row['ext_id'],'result':result,'writes':1})
            if sum(outcome.get('writes',0) for outcome in outcomes)>=limit:break
            time.sleep(3)
    return outcomes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry-known404',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    if args.retry_known404:result=retry_known_rejections()
    else:
        result=[]
        for row in cases():
            evidence=inspect(row);save_evidence(evidence)
            result.append({'id':row['ext_id'],'classification':evidence['classification'],'writes':0})
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__=='__main__':main()
