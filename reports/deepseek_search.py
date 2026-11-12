# поток: rev
"""DeepSeek Search API evidence, cached and covered by the shared daily budget.

API/pricing: https://api-docs.deepseek.com/guides/search/ (verified 2026-11-03).
Only evidence is returned here. Anthropic Batch synthesizes and verifies the answer.
"""
import hashlib
import os
import re
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse
import requests
from psycopg2.extras import Json
from core import db
from reports import recovery_budget, batch_health
from reports.prompt_privacy import public_name


def legacy_cache_key(question, product_name):
    query = ((question or '').strip()+' '+public_name(product_name or '')).strip()[:128]
    return query,hashlib.sha256(query.encode()).hexdigest()


def cache_key(question, product_name):
    # Preserve both sides of the comparison before the API's 128-character truncation.
    name=public_name(product_name or '')
    pattern=r'\b[A-Za-z]{1,6}(?:[- ][A-Za-z]{1,6})?[- ]?\d{2,6}[A-Za-z]{0,8}\b'
    asked=re.findall(pattern,question or '')[:3]
    sold=re.findall(pattern,name)[:2]
    if not asked and not sold:
        return legacy_cache_key(question,product_name)
    brands=re.findall(r'\b(?:Brother|Canon|HP|Epson|Samsung|Kyocera|Xerox|Pantum|Ricoh|OKI)\b',
                      (question or '')+' '+name,re.I)[:1]
    entities=list(dict.fromkeys(brands+asked+sold))
    query=(' '.join(entities)+' '+name+' '+(question or '').strip()).strip()[:128]
    return query,hashlib.sha256(query.encode()).hexdigest()


def cached_primary(question, product_name):
    if not db.query("SELECT to_regclass('rev_search_cache') AS name")[0]['name']:
        return None
    _, key = cache_key(question,product_name)
    _,legacy=legacy_cache_key(question,product_name)
    rows=db.query("""SELECT evidence FROM rev_search_cache WHERE query_key=ANY(%s)
        AND cached_at>now()-interval '7 days' ORDER BY (query_key=%s) DESC""",([key,legacy],key))
    return next((row['evidence'] for row in rows if row['evidence'].get('validated_primary') is True),None)


def facts(question, product_name):
    token = os.getenv('DEEPSEEK_API_KEY')
    if not token or os.getenv('FEEDBACK_LLM_PIPELINE_READY', '0') != '1':
        raise RuntimeError('DeepSeek search credentials/pipeline not validated')
    query,key = cache_key(question,product_name)
    db.execute('''CREATE TABLE IF NOT EXISTS rev_search_cache (
        query_key text PRIMARY KEY, evidence jsonb NOT NULL, cached_at timestamptz DEFAULT now())''')
    rows = db.query("SELECT evidence FROM rev_search_cache WHERE query_key=%s AND cached_at>now()-interval '7 days'", (key,))
    if rows:
        return rows[0]['evidence']
    if batch_health.blocked('deepseek'):
        raise RuntimeError('DeepSeek search temporarily paused; provider alert recorded')
    # Same search can be requested by the cycle and an operator simultaneously.
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('rev_search:'+key,))
            cached = db.query("SELECT evidence FROM rev_search_cache WHERE query_key=%s AND cached_at>now()-interval '7 days'", (key,))
            if cached:
                return cached[0]['evidence']
            return _fetch(question, query, key, token)


def _fetch(question, query, key, token):
    recovery_budget.setup()
    day = db.query("SELECT (now() AT TIME ZONE 'Europe/Moscow')::date::text AS day")[0]['day']
    # Separate search endpoint: never append /tools/search to an Anthropic-compatible URL.
    endpoint = os.getenv('DEEPSEEK_SEARCH_URL', 'https://api.deepseek.com/v1/tools/search')
    base = urlparse(os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/anthropic'))
    if base.hostname != 'api.deepseek.com' and not os.getenv('DEEPSEEK_SEARCH_URL'):
        raise RuntimeError('Custom DeepSeek provider needs an explicit search endpoint')
    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise RuntimeError('Unsafe search endpoint')
    prefix = 'deepseek-search:' + day + ':' + key
    if db.query('''SELECT 1 FROM rev_llm_budget WHERE provider='deepseek-search'
        AND request_key LIKE %s AND actual_usd IS NULL''', (prefix+'%',)):
        raise RuntimeError('Previous search outcome unconfirmed; no automatic paid retry')
    reservation = prefix + ':' + uuid.uuid4().hex
    if not recovery_budget.reserve(reservation, 'deepseek-search', '0.00075'):
        raise RuntimeError('Search budget reservation unavailable')
    try:
        response = requests.post(endpoint, headers={'Authorization':'Bearer '+token},
                                 json={'query':query,'ori_question':(question or '')[:128],
                                       'count':5,'need_content':True}, timeout=(8,45))
    except Exception as error:
        batch_health.record(error, 'deepseek')
        raise RuntimeError('DeepSeek search connection failure; alert recorded') from None
    if not response.ok:
        try: body = response.json()
        except ValueError: body = {}
        error = SimpleNamespace(status_code=response.status_code, body=body)
        if batch_health.rejected(error):
            db.execute('UPDATE rev_llm_budget SET actual_usd=0 WHERE request_key=%s', (reservation,))
        batch_health.record(error, 'deepseek')
        raise RuntimeError('DeepSeek search HTTP '+str(response.status_code))
    try:
        data=response.json()
        results=data.get('results') or []
        if not isinstance(results,list) or any(not isinstance(item,dict) for item in results):
            raise ValueError('Invalid search results')
    except (ValueError, AttributeError):
        batch_health.record(SimpleNamespace(status_code=None,body={}), 'deepseek')
        raise RuntimeError('DeepSeek search response invalid; reservation retained for reconciliation') from None
    sources = [{'title':r.get('title',''),'url':r.get('url','')} for r in results]
    evidence = {'answer':'\n\n'.join((r.get('title','')+'\n'+r.get('url','')+'\n'+
                 (r.get('content') or r.get('summary') or r.get('snippet') or '')[:5000]) for r in results),
                'sources':sources,'note':'DeepSeek Search: external evidence, not product-card facts',
                'verdict':'unclear','reply':''}
    evidence['documents']=[{'url':r.get('url',''),'title':r.get('title',''),
                           'text':(r.get('content') or r.get('summary') or r.get('snippet') or '')[:8000]}
                          for r in results]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO rev_search_cache(query_key,evidence) VALUES(%s,%s)
                ON CONFLICT(query_key) DO UPDATE SET evidence=EXCLUDED.evidence,cached_at=now()''', (key,Json(evidence)))
            cur.execute('UPDATE rev_llm_budget SET actual_usd=%s WHERE request_key=%s', ('0.00075' if results else '0',reservation))
    batch_health.clear('deepseek', source='request')
    return evidence


def targeted(query,max_age_hours=168):
    """Preserve the exact firmware/model at the front of a bounded query."""
    query=' '.join(str(query).split())[:128]
    key=hashlib.sha256(('targeted-v2:'+query).encode()).hexdigest()
    token=os.getenv('DEEPSEEK_API_KEY')
    if not token or os.getenv('FEEDBACK_LLM_PIPELINE_READY','0')!='1':
        raise RuntimeError('Search configuration unavailable')
    if batch_health.blocked('deepseek'):raise RuntimeError('Search provider paused')
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('rev_search:'+key,))
            rows=db.query("SELECT evidence,cached_at FROM rev_search_cache WHERE query_key=%s AND cached_at>now()-(%s*interval '1 hour')",(key,max_age_hours))
            if rows:return dict(rows[0]['evidence'],searched_at=str(rows[0]['cached_at']),cache_hit=True)
            result=_fetch(query,query,key,token)
            from datetime import datetime,timezone
            return dict(result,searched_at=datetime.now(timezone.utc).isoformat(),cache_hit=False)
