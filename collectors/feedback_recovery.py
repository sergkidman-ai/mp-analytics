"""Bounded, read-only marketplace recovery using the existing record mappings.

Does not generate LLM calls, send Telegram messages or publish marketplace answers.
"""
import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid

import requests
from psycopg2.extras import Json

WORKTREE = Path(__file__).resolve().parents[1]
OUT = Path('/root/rev-recovery')
sys.path.insert(0, str(WORKTREE))
from core import db
from collectors import wb_feedbacks as wb, ozon_feedbacks as oz
from collectors import yandex_feedbacks as yf, yandex_questions as yq

os.umask(0o077)
PAGE_SIZE = 20
MAX_PAGES = 2
COLS = ['item_id', 'article', 'product_name', 'rating', 'body', 'pros', 'cons',
        'created_at', 'is_answered', 'answer_text', 'status', 'payload']
report = []
records = []
RECORDS_WRITTEN = 0
LOOKBACK_DAYS = 30
KEEP_RECORDS = True


def read(method, url, **kwargs):
    for attempt in range(5):
        try:
            response = requests.request(method, url, timeout=(8, 60), **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
            continue
        if response.status_code not in (420, 429, 500, 502, 503, 504):
            break
        if attempt < 4:
            time.sleep(min(30, max(2 ** attempt, int(response.headers.get('Retry-After', '0')))))
    if not response.ok:
        raise RuntimeError('HTTP_' + str(response.status_code))
    if len(response.content) > 1024 * 1024:
        archive = OUT / 'large-responses'
        archive.mkdir(exist_ok=True, mode=0o700)
        path = archive / (uuid.uuid4().hex + '.json')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(response.content)
    return response.json()


def persist(rows):
    global RECORDS_WRITTEN
    if rows:
        db.upsert('raw_feedback', rows, ['platform', 'account', 'kind', 'ext_id'], COLS)
        RECORDS_WRITTEN += len(rows)
        if not KEEP_RECORDS:
            return
        for row in rows:
            row = dict(row)
            if isinstance(row.get('payload'), Json):
                row['payload'] = row['payload'].adapted
            records.append(row)


def stage(label, callback):
    before = RECORDS_WRITTEN
    try:
        callback()
        result = {'channel': label, 'status': 'ok', 'rows': RECORDS_WRITTEN - before}
    except Exception as error:
        # Never include exception URLs, headers or customer content in logs.
        detail = str(error) if isinstance(error, RuntimeError) and str(error).startswith('HTTP_') else type(error).__name__
        result = {'channel': label, 'status': 'failed', 'error': detail, 'rows': RECORDS_WRITTEN - before}
    report.append(result)
    print(json.dumps(result), flush=True)
    (OUT / 'collection-progress.json').write_text(json.dumps(report, indent=2))


def collect_wb(account, kind):
    path, field, mapping = ('/feedbacks', 'feedbacks', wb._feedback_rec) if kind == 'review' else ('/questions', 'questions', wb._question_rec)
    token = wb.feedback_token(account)
    for answered in ['false', 'true']:
        for page in range(MAX_PAGES):
            params = {'isAnswered': answered, 'take': PAGE_SIZE, 'skip': page * PAGE_SIZE, 'order': 'dateDesc'}
            if LOOKBACK_DAYS is not None and answered == 'true':
                params['dateFrom'] = int(time.time()) - LOOKBACK_DAYS * 86400
            data = read('GET', wb.BASE + path, headers={'Authorization': token}, params=params)
            items = (data.get('data') or {}).get(field) or []
            rows = [mapping(account, item) for item in items]
            for row in rows:
                # The answered-list filter is authoritative even when answer text is hidden.
                row['is_answered'] = answered == 'true' or row['is_answered']
            persist(rows)
            print(json.dumps({'page': page + 1, 'channel': account + ':' + kind, 'answered_filter': answered, 'received': len(items)}), flush=True)
            time.sleep(1.1)
            if len(items) < PAGE_SIZE:
                break


def collect_ozon(account):
    headers = oz._headers(account)
    names = oz._names(account)
    last = ''
    seen = set()
    for _ in range(MAX_PAGES):
        body = {'limit': PAGE_SIZE}
        if last:
            body['last_id'] = last
        data = read('POST', oz.QUESTION_URL, headers=headers, json=body)
        items = data.get('questions') or []
        rows = []
        for item in items:
            mine=None;answer_records=[]
            if item.get('status')=='NEW' and (item.get('answers_count') or 0)>0:
                from collectors.ozon_answer_owner import own_reply
                mine,answer_records=own_reply(account,item['id'],item.get('sku'),read=read)
                item=dict(item,recovery_answers=answer_records)
            answered=bool(mine) if item.get('status')=='NEW' else (item.get('answers_count') or 0)>0
            rows.append({'platform': 'ozon', 'account': account, 'kind': 'question', 'ext_id': str(item['id']),
                         'item_id': str(item.get('sku') or ''), 'article': None,
                         'product_name': names.get(str(item.get('sku'))), 'rating': None,
                         'body': item.get('text') or '', 'pros': None, 'cons': None,
                         'created_at': item.get('published_at'), 'is_answered': answered,
                         'answer_text': (mine or {}).get('text'), 'status': item.get('status'), 'payload': Json(item)})
        persist(rows)
        if rows:
            db.execute("""UPDATE raw_feedback SET recovery_active_unanswered=(status='NEW' AND NOT is_answered),
                recovery_status_checked_at=now() WHERE platform='ozon' AND account=%s AND kind='question' AND ext_id=ANY(%s)""",
                (account,[r['ext_id'] for r in rows]))
        print(json.dumps({'page': _ + 1, 'channel': account + ':question', 'received': len(items)}), flush=True)
        last = data.get('last_id') or ''
        if not items or not last or len(items) < PAGE_SIZE:
            break
        if last in seen:
            raise RuntimeError('RepeatedCursor')
        seen.add(last)
        time.sleep(0.5)


def collect_yandex(kind):
    from collectors.yandex_feedback_coverage import question_jobs,review_jobs
    key, business = yq._cfg()
    names = yq._names()
    shops = yq._shop_names()
    now=datetime.datetime.now(datetime.timezone.utc)
    if kind=='question':
        pending=db.query("SELECT created_at FROM raw_feedback WHERE platform='yandex' AND account='ya_acc1' AND kind='question' AND NOT is_answered")
        jobs=question_jobs(now,[r['created_at'] for r in pending])
    else:
        older=db.query("""SELECT ext_id FROM raw_feedback WHERE platform='yandex' AND account='ya_acc1'
            AND kind='review' AND NOT is_answered AND created_at<%s""",(now-datetime.timedelta(days=179),))
        jobs=review_jobs(now,[r['ext_id'] for r in older])
    for body in jobs:
        _collect_yandex_job(kind,key,business,names,shops,body)


def _collect_yandex_job(kind,key,business,names,shops,body):
    token=None;seen=set()
    endpoint=('/v2/businesses/'+business+'/goods-feedback') if kind=='review' else ('/v1/businesses/'+business+'/goods-questions')
    for _ in range(MAX_PAGES):
        params = {'limit': PAGE_SIZE}
        if token:
            params['pageToken'] = token
        data = read('POST', yq.API + endpoint, headers={'Api-Key': key}, json=body, params=params).get('result') or {}
        items = data.get('feedbacks' if kind == 'review' else 'questions') or []
        rows = []
        unavailable = []
        for item in items:
            if kind == 'review':
                stats = item.get('statistics') or {}
                text, pros, cons = yf._text_parts(item.get('description'))
                offer = str((item.get('identifiers') or {}).get('offerId') or '')
                identifier = str(item['feedbackId'])
                try:
                    mine, comments = yandex_review_answer(key, business, str(item['feedbackId']), stats.get('commentsCount') or 0)
                except RuntimeError as error:
                    if str(error) != 'HTTP_404':
                        raise
                    mine, comments = None, []
                    unavailable.append(str(item['feedbackId']))
                    item = dict(item, recovery_comments_error='HTTP_404')
                answered = bool(mine)
                answer = (mine or {}).get('text')
                item = dict(item, recovery_comments=comments)
                rating = stats.get('rating')
            else:
                ids = item.get('questionIdentifiers') or {}
                identifier, offer = str(ids.get('id')), str(ids.get('offerId') or '')
                answers = read('POST', yq.API + '/v1/businesses/' + business + '/goods-questions/answers',
                               headers={'Api-Key': key}, json={'questionId': int(identifier)}).get('result', {}).get('answers') or []
                mine = yq._our_answer(answers, shops)
                item = dict(item, answers=answers)
                answered, answer = bool(mine), (mine or {}).get('text')
                text, pros, cons, rating = (item.get('text') or '').strip(), None, None, None
                time.sleep(0.3)
            rows.append({'platform': 'yandex', 'account': 'ya_acc1', 'kind': kind, 'ext_id': identifier,
                         'item_id': offer, 'article': offer or None, 'product_name': names.get(offer),
                         'rating': rating, 'body': text, 'pros': pros, 'cons': cons, 'created_at': item.get('createdAt'),
                         'is_answered': answered, 'answer_text': answer, 'status': None, 'payload': Json(item)})
        persist(rows)
        if rows:
            db.execute("""UPDATE raw_feedback SET recovery_status_checked_at=now()
                WHERE platform='yandex' AND account='ya_acc1' AND kind=%s AND ext_id=ANY(%s)""",
                (kind,[r['ext_id'] for r in rows]))
        if unavailable:
            db.execute("""UPDATE raw_feedback SET skipped_no_api=true,status='comments_unavailable_404'
                WHERE platform='yandex' AND account='ya_acc1' AND kind='review' AND ext_id=ANY(%s)""", (unavailable,))
        print(json.dumps({'page': _ + 1, 'channel': 'ya_acc1:' + kind, 'received': len(items)}), flush=True)
        token = (data.get('paging') or {}).get('nextPageToken')
        if not items or not token:
            break
        if token in seen:
            raise RuntimeError('RepeatedCursor')
        seen.add(token)
        time.sleep(0.5)
    else:
        raise RuntimeError('YandexPaginationIncomplete')


def yandex_review_answer(key, business, identifier, expected_count=None):
    """Own response is identified by API canModify, not by any comment count."""
    if expected_count==0:
        return None,[]
    if expected_count is not None:
        cached = db.query("""SELECT payload FROM raw_feedback WHERE platform='yandex' AND account='ya_acc1'
            AND kind='review' AND ext_id=%s AND recovery_status_checked_at>now()-interval '15 minutes'
            AND payload ? 'recovery_comments'""", (identifier,))
        if cached and (cached[0]['payload'].get('statistics') or {}).get('commentsCount',0) == expected_count:
            comments = cached[0]['payload']['recovery_comments']
            mine = next((c for c in reversed(comments) if c.get('canModify') is True
                         and c.get('status') in ('PUBLISHED','UNMODERATED')), None)
            return mine, comments
    comments, token, seen = [], None, set()
    while True:
        started = time.monotonic()
        params = {'limit':20}
        if token:
            params['pageToken'] = token
        result = read('POST', yq.API + '/v2/businesses/' + business + '/goods-feedback/comments',
                      headers={'Api-Key':key},json={'feedbackId':int(identifier)},params=params).get('result') or {}
        comments.extend(result.get('comments') or [])
        token = (result.get('paging') or {}).get('nextPageToken')
        time.sleep(max(0, 0.8 - (time.monotonic() - started)))
        if not token:
            break
        if token in seen:
            raise RuntimeError('RepeatedCommentCursor')
        seen.add(token)
    mine = next((c for c in reversed(comments) if c.get('canModify') is True
                 and c.get('status') in ('PUBLISHED','UNMODERATED')), None)
    return mine, comments


if __name__ == '__main__':
    assert db.query('SELECT current_database() AS name')[0]['name'] == 'rev_recovery_20261103'
    if '--replay' in sys.argv:
        rows = json.loads((OUT / 'recovered-records.json').read_text())
        for row in rows:
            row['payload'] = Json(row['payload'])
        persist(rows)
        print('Replayed', len(rows), 'records using existing UPSERT; no publication')
    else:
        for account in ['wb_acc1', 'wb_acc2']:
            for kind in ['review', 'question']:
                stage(account + ':' + kind, lambda a=account, k=kind: collect_wb(a, k))
        for account in ['oz_acc1', 'oz_acc2']:
            stage(account + ':question', lambda a=account: collect_ozon(a))
        for kind in ['review', 'question']:
            stage('ya_acc1:' + kind, lambda k=kind: collect_yandex(k))
        (OUT / 'recovered-records.json').write_text(json.dumps(records, ensure_ascii=False))
        (OUT / 'collection-result.json').write_text(json.dumps({'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'page_size': PAGE_SIZE, 'max_pages_per_state': MAX_PAGES, 'wb_window_days': 30,
            'full_history_restored': False, 'stages': report, 'records': len(records)}, indent=2))
