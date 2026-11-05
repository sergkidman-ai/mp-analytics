# поток: rev
"""Identify a seller answer rather than counting answers from other buyers."""
import requests
from core import db
from collectors.ozon_feedbacks import _headers


def is_own(answer,names):
    return ((answer.get('author_name') or '').strip().casefold() in
            {name.strip().casefold() for name in names}
            and (answer.get('status_publication') or '').upper() not in
            {'BANNED','REJECTED','DELETED','FAILED','NOT_PUBLISHED'})


def own_reply(account,question_id,sku,read=None):
    identities=db.query('SELECT author_name FROM rev_ozon_answer_identity WHERE account=%s',(account,))
    names={r['author_name'] for r in identities}
    if not names:
        raise RuntimeError('OzonSellerIdentityUnverified')
    if read is None:
        def read(method,url,**kwargs):
            response=requests.request(method,url,timeout=(8,25),**kwargs)
            if not response.ok:raise RuntimeError('HTTP_'+str(response.status_code))
            return response.json()
    cursor='';seen=set();answers=[]
    for _ in range(1000):
        body={'question_id':question_id,'sku':int(sku),'limit':20}
        if cursor:body['last_id']=cursor
        data=read('POST','https://api-seller.ozon.ru/v1/question/answer/list',
                  headers=_headers(account),json=body)
        batch=data.get('answers') or []
        answers.extend(batch)
        cursor=data.get('last_id') or ''
        if not batch or not cursor or len(batch)<20:break
        if cursor in seen:raise RuntimeError('RepeatedOzonAnswerCursor')
        seen.add(cursor)
    else:raise RuntimeError('OzonAnswerPaginationIncomplete')
    return next((answer for answer in answers if is_own(answer,names)),None),answers
