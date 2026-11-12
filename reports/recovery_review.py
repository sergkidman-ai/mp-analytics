# поток: rev
"""Resume Batch processing of delivered recovery cards, editing their original messages.

The normal engine remains responsible for answers. This adapter protects operator decisions,
holds a shared card lock during updates, and records unfinished edits for the next cycle.
"""
import hashlib
import json
import re
from pathlib import Path
import uuid

from psycopg2.extras import Json
from core import db
from feedback_bot import recovery_runtime as runtime, tg_moderation as bot
from reports import feedback_today as engine, llm_batch, publish_gate, request_class


def setup():
    runtime.setup()
    db.execute('''CREATE TABLE IF NOT EXISTS rev_card_reprocess(
        mod_id bigint PRIMARY KEY, state text NOT NULL DEFAULT 'pending',
        error_type text, updated_at timestamptz NOT NULL DEFAULT now())''')


def source_hash(row):
    return hashlib.md5(''.join(row.get(k) or '' for k in ['body','pros','cons']).encode()).hexdigest()


def eligible(row):
    return (row['state'] in ('carded','queued') and row.get('final_text') is None
            and row.get('decided_at') is None and not row.get('editing')
            and not row.get('is_answered') and not row.get('posted_ok')
            and not row.get('skipped_old') and not row.get('skipped_no_api')
            and row.get('recovery_active_unanswered') is not False)


def unsupported_origins(reply,evidence):
    words=re.findall(r'кита[йеяю]\w*|росси\w*|коре\w*|германи\w*|япони\w*|вьетнам\w*|тайван\w*',reply.lower())
    return [word for word in words if word not in evidence.lower()]


def owner_actions(question):
    if re.search(r'фото.*упаков|упаков.*фото',question or '',re.I):
        return ['нужно предоставить реальное фото упаковки; условия оплаты и гарантии подтвердить отдельно']
    return []


def load(mod_id):
    rows = db.query('''SELECT f.*,m.id AS mod_id,m.state,m.final_text,m.decided_at,
        m.tg_chat_id,m.tg_msg_id,
        EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id) AS editing
        FROM feedback_moderation m JOIN raw_feedback f USING(platform,account,kind,ext_id)
        WHERE m.id=%s''', (mod_id,))
    return rows[0] if rows else None


def mark(mod_id, state, error=None):
    db.execute('''UPDATE rev_card_reprocess SET state=%s,error_type=%s,updated_at=now()
        WHERE mod_id=%s''', (state,error,mod_id))


def stage_candidate(original, reply, ground):
    """Show a completed draft while its Batch controller is pending, without send buttons."""
    mid = original.get('mod_id')
    if not mid or not (reply or '').strip():
        return False
    with runtime.card_lock(mid):
        current = load(mid)
        if not current or not eligible(current) or source_hash(current) != source_hash(original):
            return False
        pending = dict(ground, verify='pending', recovery_reprocessed=True,
                       note='Ответ подготовлен; ожидается проверка контролёра Anthropic Batch.')
        key = tuple(current[k] for k in ['platform','account','kind','ext_id'])
        db.execute('''UPDATE raw_feedback SET draft_text=%s,draft_route='review',
            draft_grounding=%s,draft_at=now()
            WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''', (reply,Json(pending),*key))
        if current.get('tg_msg_id'):
            current.update(id=mid,draft_text=reply,draft_grounding=pending,draft_route='review')
            try:
                response=bot.api('editMessageText',{'chat_id':current['tg_chat_id'],
                    'message_id':current['tg_msg_id'],'text':bot._card(current)[:4000],
                    'parse_mode':'HTML','disable_web_page_preview':True,
                    'reply_markup':bot._kb(mid,allow_send=False,allow_override=False)})
                return bool(response.get('ok'))
            except Exception:
                return False  # Retry the same edit on the next tick; never send another card.
        return True


def save(mod_id, original, result):
    _, reply, route, confidence, ground, _, _ = result
    # Human markers belong in the operator note, never in the buyer's answer field.
    if reply.lstrip().startswith('⚠️'):
        ground = dict(ground, note=reply + '; ' + (ground.get('note') or ''))
        reply, route = '', 'human'
    route, ground = engine._auto_card_q(original, reply, route, ground)
    actions=owner_actions(original.get('body')) if original['kind']=='question' else []
    if actions:
        route='human'
        ground=dict(ground,owner_actions=actions,fact_block='; '.join(actions))
    if original['kind']=='question':
        f = engine._card_facts(engine.CardFacts(),original) or {}
        native = ' '.join(str(f.get(key) or '') for key in ['name','annot','country','manufacturer'])
        expert, _ = engine.bn.facts_for(engine._brand_of(original),original.get('body'),native)
        evidence = ' '.join([native or '',expert or '',ground.get('verification_evidence') or '']).lower()
        unsupported = unsupported_origins(reply,evidence)
        if unsupported:
            ground = dict(ground,fact_block='страна производства не подтверждена источником')
            route = 'human'
    sample = db.query('''SELECT min(m.id) AS id FROM feedback_moderation m
        JOIN raw_feedback f USING(platform,account,kind,ext_id)
        WHERE m.state='carded' AND f.kind='review' AND f.rating>=4 AND NOT f.is_answered
          AND NOT coalesce(f.posted_ok,false)''')[0]['id']
    from reports.simple_review_policy import matches as simple_matches
    simple = simple_matches(dict(original, draft_grounding=ground), reply)
    if reply and not simple and original['kind']=='review' and mod_id==sample:
        route = 'review' if route!='human' else route
        ground = dict(ground,operator_sample=True)
    cls = request_class.classify(' '.join(filter(None,[original.get('body'),original.get('pros'),original.get('cons')]))
                                if original['kind']=='review' else original.get('body'),
                                kind=original['kind'],rating=original.get('rating'))
    ground = dict(ground, request_class=cls, recovery_reprocessed=True)
    with runtime.card_lock(mod_id):
        current = load(mod_id)
        if not current or not eligible(current):
            mark(mod_id,'preserved_decision')
            return False
        if source_hash(current) != source_hash(original):
            mark(mod_id,'pending','SourceChanged')
            return False
        key = tuple(current[k] for k in ['platform','account','kind','ext_id'])
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('''UPDATE raw_feedback SET draft_text=%s,draft_route=%s,
                    draft_confidence=%s,draft_grounding=%s,draft_at=now(),
                    request_class=%s,template_id=%s,draft_category=%s,draft_src_hash=%s
                    WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',
                    (reply,route,confidence,Json(ground),cls,ground.get('template_id') or ('llm' if ground.get('llm') else None),
                     original['kind'],source_hash(current),*key))
                cur.execute('UPDATE feedback_moderation SET recovery_revision=%s WHERE id=%s',
                            (uuid.uuid4().hex[:10],mod_id))
                cur.execute("UPDATE rev_card_reprocess SET state='edit_pending',updated_at=now(),error_type=NULL WHERE mod_id=%s",(mod_id,))
        return refresh(mod_id, already_locked=True)


def refresh(mod_id, already_locked=False):
    if not already_locked:
        with runtime.card_lock(mod_id):
            return refresh(mod_id, already_locked=True)
    row = load(mod_id)
    if not row or not eligible(row):
        mark(mod_id,'preserved_decision')
        return False
    row['id'] = mod_id
    if not row.get('tg_msg_id'):
        mark(mod_id,'done')  # First delivery waits for a completed draft in send_cards().
        return True
    allowed, reasons, _ = publish_gate.verdict_full(row)
    markup = bot._kb(mod_id,allow_send=allowed,
                     allow_override=not allowed and publish_gate.can_override(reasons))
    # An edit retry is idempotent. Treat "message is not modified" as a confirmed edit.
    try:
        response = bot.api('editMessageText',{'chat_id':row['tg_chat_id'],'message_id':row['tg_msg_id'],
            'text':bot._card(row)[:4000], 'parse_mode':'HTML','disable_web_page_preview':True,
            'reply_markup':markup})
    except Exception as error:
        from urllib.error import HTTPError
        if isinstance(error,HTTPError):
            try:
                detail = json.loads(error.read()).get('description','')
            except Exception:
                detail = ''
            if error.code == 400 and 'message is not modified' in detail.lower():
                mark(mod_id,'done')
                return True
        mark(mod_id,'edit_pending',type(error).__name__)
        return False
    if response.get('ok'):
        mark(mod_id,'done')
        return True
    mark(mod_id,'edit_pending','TelegramRejected')
    return False


def tick():
    setup()
    requeue_changed_facts()
    db.execute('''INSERT INTO rev_card_reprocess(mod_id)
        SELECT m.id FROM feedback_moderation m JOIN raw_feedback f USING(platform,account,kind,ext_id)
        WHERE m.state IN ('carded','queued') AND m.final_text IS NULL AND m.decided_at IS NULL
          AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
          AND NOT f.skipped_old AND NOT f.skipped_no_api
          AND coalesce(f.recovery_active_unanswered,true)
        ON CONFLICT DO NOTHING''')
    facts, corpus, client = engine.CardFacts(), engine.load_corpus(), engine._client()
    from reports import batch_health
    from reports.simple_review_policy import answer as simple_answer
    provider_block = batch_health.blocked()
    stats = {'pending_batch':0,'updated_existing_cards':0,'preserved':0,'errors':0,'provider_blocked':0}
    for job in db.query("SELECT mod_id,state FROM rev_card_reprocess WHERE state IN ('pending','edit_pending') ORDER BY mod_id"):
        mid = job['mod_id']
        if job['state']=='edit_pending':
            stats['updated_existing_cards'] += bool(refresh(mid))
            continue
        row = load(mid)
        if not row or not eligible(row):
            mark(mid,'preserved_decision');stats['preserved']+=1
            continue
        if provider_block and not simple_answer(row):
            stats['provider_blocked'] += 1
            continue
        try:
            if row['kind']=='question' and re.search(r'когда.*(?:налич|появи|поступ)',row.get('body') or '',re.I):
                ground={'source':'владелец','grounded':False,'fact_block':'дата поступления не подтверждена',
                        'note':'Нужна фактическая дата поступления от владельца; активная карточка не подтверждает наличие.'}
                result=({},'','human',0,ground,False,False)
            else:
                result = engine._answer(client,row,facts,corpus)
        except llm_batch.LlmPending:
            mark(mid,'pending')
            stats['pending_batch']+=1
            continue
        except Exception as error:
            mark(mid,'pending',type(error).__name__);stats['errors']+=1
            continue
        stats['updated_existing_cards'] += bool(save(mid,row,result))
    Path('/root/rev-recovery/reprocess-latest.json').write_text(json.dumps(stats,indent=2))
    return stats


def requeue_changed_facts():
    """Invalidate stale PASS under the same lock as operator edits. No paid calls."""
    from reports.card_facts import FACTS_VERSION
    from reports.card_fact_validation import signature
    facts = engine.CardFacts()
    changed = 0
    ids = db.query('''SELECT m.id FROM feedback_moderation m JOIN raw_feedback f
        USING(platform,account,kind,ext_id) WHERE f.kind='question' AND m.state IN ('carded','queued')
        AND m.final_text IS NULL AND m.decided_at IS NULL AND NOT f.is_answered
        AND NOT coalesce(f.posted_ok,false) AND NOT f.skipped_old
        AND coalesce(f.recovery_active_unanswered,true)''')
    for item in ids:
        mid = item['id']
        with runtime.card_lock(mid):
            row = load(mid)
            if not row or not eligible(row):
                continue
            sig = signature(engine._card_facts(facts,row))
            ground = row.get('draft_grounding') or {}
            if ground.get('facts_version') == FACTS_VERSION and ground.get('facts_signature') == sig:
                continue
            if ground.get('facts_pending_signature') == sig and ground.get('facts_pending_version') == FACTS_VERSION:
                continue
            pending = dict(ground, facts_checked=False, facts_pending_signature=sig,
                           facts_pending_version=FACTS_VERSION,
                           fact_block='обновлены факты карточки; черновик проходит повторную проверку')
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('''UPDATE raw_feedback SET draft_grounding=%s,draft_route='review'
                        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',
                        (Json(pending),*(row[k] for k in ('platform','account','kind','ext_id'))))
                    cur.execute('''INSERT INTO rev_card_reprocess(mod_id,state) VALUES(%s,'pending')
                        ON CONFLICT(mod_id) DO UPDATE SET state='pending',error_type=NULL,updated_at=now()''',(mid,))
            changed += 1
    return changed
