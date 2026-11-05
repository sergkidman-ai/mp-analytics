# поток: rev
"""Bounded historical analysis through the existing engine; never publishes or sends cards.

Enrollment is explicit. The normal short tick resumes the enrolled work, under the shared
budget. skipped_old remains set, so preparation cannot enable automatic publication.
"""
import hashlib
from pathlib import Path

from psycopg2.extras import Json
from core import db
from reports import feedback_today as engine, llm_batch, llm_routing
from reports.feedback_draft_run import draft_review, _first_name, _short

ROOT=Path(__file__).resolve().parents[1]
KEYS=['platform','account','kind','ext_id']


def setup():
    db.apply_sql_file(ROOT/'migrations/716_rev_history_analysis.sql')


def signature(row):
    return hashlib.md5(''.join(row.get(k) or '' for k in ['body','pros','cons']).encode()).hexdigest()


def template_only(row):
    if row['kind']!='review':
        return False
    text=' '.join(filter(None,[row.get('body'),row.get('pros'),row.get('cons')])).strip()
    if not text:
        return True
    needs,_=llm_routing.needs_llm('review',text,None,row.get('rating'))
    return engine._positive_clean(row) and not needs and '?' not in text


def enroll():
    """Owner-authorized, idempotent snapshot; never clears archive or operator decisions."""
    setup()
    rows=db.query('''SELECT f.* FROM raw_feedback f WHERE f.platform='yandex'
        AND f.skipped_old AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
        AND f.posted_at IS NULL''')
    jobs=[dict(**{k:r[k] for k in KEYS},state='pending',
               classification='template' if template_only(r) else 'semantic',source_hash=signature(r)) for r in rows]
    db.upsert('rev_history_analysis',jobs,KEYS,update_cols=[])
    return summary()


def protected(row):
    return (row.get('is_answered') or row.get('posted_ok') or row.get('posted_at') is not None
            or row.get('final_text') is not None or row.get('decided_at') is not None
            or row.get('moderation_state') in ('skipped','sent','external_answered','edited')
            or row.get('editing'))


def mark(key,state,error=None):
    db.execute('''UPDATE rev_history_analysis SET state=%s,error_type=%s,updated_at=now()
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',(state,error,*key))


def store(row,reply,ground,confidence,category,template=None,route='review'):
    """The guarded transaction preserves manual work and holds every historical publication."""
    key=tuple(row[k] for k in KEYS)
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            # Same per-card lock as Telegram if an archived record already had a card.
            if row.get('existing_mod_id'):
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('rev_card:'+str(row['existing_mod_id']),))
            cur.execute('''UPDATE raw_feedback f SET draft_text=%s,draft_route=%s,
                draft_confidence=%s,draft_grounding=%s,draft_category=%s,template_id=%s,
                draft_src_hash=%s,draft_at=now()
                WHERE (f.platform,f.account,f.kind,f.ext_id)=(%s,%s,%s,%s)
                AND f.skipped_old AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
                AND f.posted_at IS NULL
                AND md5(coalesce(f.body,'')||coalesce(f.pros,'')||coalesce(f.cons,''))=%s
                AND NOT EXISTS(SELECT 1 FROM feedback_moderation m
                    WHERE (m.platform,m.account,m.kind,m.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                    AND (m.final_text IS NOT NULL OR m.decided_at IS NOT NULL
                         OR m.state IN ('skipped','sent','external_answered','edited')))
                AND NOT EXISTS(SELECT 1 FROM rev_edit_session e JOIN feedback_moderation m ON m.id=e.mod_id
                    WHERE (m.platform,m.account,m.kind,m.ext_id)=(f.platform,f.account,f.kind,f.ext_id))''',
                (reply,'human' if not reply or route=='human' else 'review',confidence,Json(dict(ground,
                    historical_analysis=True,historical_publication_requires_owner=True)),
                    category,template,signature(row),*key,signature(row)))
            state=('prepared' if reply and route!='human' else 'needs_facts') if cur.rowcount else 'preserved'
            cur.execute('''UPDATE rev_history_analysis SET state=%s,error_type=NULL,updated_at=now()
                WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',(state,*key))
    return state


def cases(classification,limit):
    return db.query('''SELECT f.*,h.classification,h.source_hash AS enrolled_hash,
        m.id AS existing_mod_id,m.state AS moderation_state,m.final_text,m.decided_at,
        EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id) AS editing
        FROM rev_history_analysis h JOIN raw_feedback f USING(platform,account,kind,ext_id)
        LEFT JOIN feedback_moderation m USING(platform,account,kind,ext_id)
        WHERE h.state IN ('pending','batch_pending','retry') AND h.classification=%s
        ORDER BY f.created_at DESC LIMIT %s''',(classification,limit))


def tick(limit=8):
    setup()
    stats={'templates_prepared':0,'semantic_prepared':0,'batch_pending':0,'preserved':0,'errors':0}
    for row in cases('template',10000):
        key=tuple(row[k] for k in KEYS)
        if protected(row) or signature(row)!=row['enrolled_hash']:
            mark(key,'preserved');stats['preserved']+=1;continue
        category,reply,_,confidence,template=draft_review(row,_first_name(row.get('payload')),_short(row.get('product_name') or ''))
        ground={'source':'действующий шаблон','template_id':template,'grounded':True,
                'verify':'not_required','note':'Историческая оценка: только черновик; публикация не включена.'}
        stats['templates_prepared']+=store(row,reply,ground,confidence,category,template)=='prepared'
    work=cases('semantic',limit)
    if work:
        from reports.archive_question_policy import is_archive_question
        facts=engine.CardFacts()
        needs_paid_engine=any(not is_archive_question(row) for row in work)
        corpus=engine.load_corpus() if needs_paid_engine else None
        client=engine._client() if needs_paid_engine else None
    for row in work:
        key=tuple(row[k] for k in KEYS)
        if protected(row) or signature(row)!=row['enrolled_hash']:
            mark(key,'preserved');stats['preserved']+=1;continue
        try:
            _,reply,route,confidence,ground,_,_=engine._answer(client,row,facts,corpus)
            if reply.lstrip().startswith('⚠️'):
                ground=dict(ground,note=reply);reply=''
            state=store(row,reply,ground,confidence,row['kind'],ground.get('template_id'),route)
            stats['semantic_prepared']+=state=='prepared'
        except llm_batch.LlmPending:
            mark(key,'batch_pending');stats['batch_pending']+=1
        except Exception as error:
            mark(key,'retry',type(error).__name__);stats['errors']+=1
    return dict(stats,queue=summary())


def summary():
    return db.query('''SELECT classification,state,count(*) AS n FROM rev_history_analysis
        GROUP BY 1,2 ORDER BY 1,2''')
