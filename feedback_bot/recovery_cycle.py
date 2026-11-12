# поток: rev
"""Regular recovery cycle: read marketplaces, prepare drafts, moderate; fence publications."""
import datetime
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import db
from collectors import feedback_recovery as collect
from feedback_bot import tg_moderation as bot, recovery_runtime as runtime
from reports import recovery_budget, publish_gate

OUT = Path('/root/rev-recovery')


def refresh_wb_active():
    """Read all unanswered pages; do not invent historical publication timestamps."""
    status = []
    for account in ['wb_acc1', 'wb_acc2']:
        for kind, route, field in [('review', '/feedbacks', 'feedbacks'), ('question', '/questions', 'questions')]:
            identifiers, skip = [], 0
            while True:
                data = collect.read('GET', collect.wb.BASE + route,
                    headers={'Authorization': collect.wb.feedback_token(account)},
                    params={'isAnswered':'false','take':20,'skip':skip,'order':'dateDesc'})
                items = (data.get('data') or {}).get(field) or []
                identifiers.extend(str(item['id']) for item in items)
                if len(items) < 20:
                    break
                skip += 20
                time.sleep(1.1)
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute('''UPDATE raw_feedback SET recovery_active_unanswered=(ext_id=ANY(%s)),
                        recovery_status_checked_at=now() WHERE platform='wb' AND account=%s AND kind=%s''',
                        (identifiers, account, kind))
            status.append({'account':account,'kind':kind,'currently_unanswered_api':len(identifiers)})
            time.sleep(1.1)
    return status


def names_and_queue():
    db.execute("""UPDATE raw_feedback f SET product_name=c.title,article=c.vendor_code FROM wb_cards c
        WHERE f.platform='wb' AND f.account=c.account AND f.item_id=c.nm_id::text AND c.title IS NOT NULL""")
    db.execute("""UPDATE raw_feedback f SET product_name=c.name,article=c.offer_id FROM ozon_product c
        WHERE f.platform='ozon' AND f.account=c.account AND f.item_id=c.sku AND c.name IS NOT NULL""")
    db.execute("""UPDATE raw_feedback f SET product_name=coalesce(nullif(c.payload->'mapping'->>'marketSkuName',''),c.payload->'offer'->>'name')
        FROM raw_yandex_offer c WHERE f.platform='yandex' AND f.account=c.account AND f.item_id=c.offer_id""")


def refresh_catalog_if_stale():
    from collectors import catalog_recovery as catalog
    db.execute('''CREATE TABLE IF NOT EXISTS rev_catalog_refresh (
        channel text PRIMARY KEY, last_ok_at timestamptz, last_error text)''')
    for label,fn in [('wb_acc1',lambda:catalog.wb('wb_acc1')),('wb_acc2',lambda:catalog.wb('wb_acc2')),
                     ('oz_acc1',lambda:catalog.ozon('oz_acc1')),('oz_acc2',lambda:catalog.ozon('oz_acc2')),('ya_acc1',catalog.yandex)]:
        fresh=db.query("SELECT 1 FROM rev_catalog_refresh WHERE channel=%s AND last_ok_at>now()-interval '24 hours'",(label,))
        if fresh:
            continue
        try:
            fn()
            db.execute('''INSERT INTO rev_catalog_refresh(channel,last_ok_at) VALUES(%s,now())
                ON CONFLICT(channel) DO UPDATE SET last_ok_at=now(),last_error=NULL''',(label,))
        except Exception as error:
            db.execute('''INSERT INTO rev_catalog_refresh(channel,last_error) VALUES(%s,%s)
                ON CONFLICT(channel) DO UPDATE SET last_error=EXCLUDED.last_error''',(label,type(error).__name__))
            print('Catalog refresh pending: '+label+' '+type(error).__name__,flush=True)


def queue_cases():
    db.execute('''INSERT INTO feedback_moderation(platform,account,kind,ext_id)
        SELECT platform,account,kind,ext_id FROM raw_feedback
        WHERE NOT is_answered AND coalesce(recovery_active_unanswered,true)
        AND NOT skipped_no_api AND NOT skipped_old AND posted_at IS NULL
        ON CONFLICT(platform,account,kind,ext_id) DO NOTHING''')
    rows = db.query('''SELECT m.id,m.tg_chat_id,m.tg_msg_id,f.posted_ok,coalesce(m.final_text,f.draft_text) AS draft_text FROM feedback_moderation m JOIN raw_feedback f
        USING(platform,account,kind,ext_id)
        WHERE (f.is_answered OR f.posted_ok OR NOT coalesce(f.recovery_active_unanswered,true))
        AND (m.state IN ('queued','carded','snoozed','approved_blocked','edited','failed')
             OR (m.state='sent' AND NOT m.recovery_sent_card_ok)
             OR (m.state='publication_review' AND EXISTS(SELECT 1 FROM rev_publication_attempt a
                 WHERE (a.platform,a.account,a.kind,a.ext_id)=(m.platform,m.account,m.kind,m.ext_id)
                 AND a.state='confirmed')))''')
    for row in rows:
        if row['tg_chat_id'] and row['tg_msg_id']:
            import html
            text = ('✅ Отправлено\n\n'+html.escape(row['draft_text'] or '')) if row['posted_ok'] else 'Обращение уже отвечено или снято с активной очереди площадки. Повторная отправка отключена.'
            response = bot.edit_text(row['tg_chat_id'],row['tg_msg_id'],text)
            if not response or not response.get('ok'):
                continue
        bot._set(row['id'], 'sent' if row['posted_ok'] else 'external_answered',
                 recovery_sent_card_ok=bool(row['posted_ok']))


def publication_phase():
    if runtime.publication_block():
        return {'blocked':runtime.publication_block(),'automatic':0}
    # Operator-approved texts are durable, and the normal dispatcher rechecks source state,
    # publication eligibility and the per-case attempt ledger before any marketplace request.
    for row in db.query("SELECT id,final_text,decided_by,tg_chat_id,tg_msg_id FROM feedback_moderation WHERE state='approved_blocked' ORDER BY decided_at"):
        bot._do_send(row['id'],str(row['decided_by']),row['final_text'],row['tg_chat_id'],row['tg_msg_id'])
    from collectors import feedback_autosend
    return {'blocked':'','automatic':feedback_autosend.run()}


def group(row):
    if row['kind'] == 'review':
        return 'negative' if (row.get('rating') or 0) <= 3 else 'positive'
    text = row.get('body') or ''
    if re.search('подбор|подберите|какой|какая|есть ли|посовет',text,re.I):
        return 'selection'
    if re.search('подойд|совмест|подход',text,re.I):
        return 'compatibility'
    return 'question'


def send_cards(sample_limit=10):
    pending = bot._pending(limit=100000, days=36500)
    from reports.simple_review_policy import matches as simple_matches
    pending = [row for row in pending if not simple_matches(row)]
    buckets = {}
    for row in pending:
        buckets.setdefault(group(row), []).append(row)
    selected = []
    while len(selected) < sample_limit and any(buckets.values()):
        for label in ['positive','negative','compatibility','selection','question']:
            if buckets.get(label) and len(selected)<sample_limit:
                selected.append(buckets[label].pop(0))
    # All genuinely human cases are delivered; ready examples remain capped at ten per cycle.
    known = {r['id'] for r in selected}
    selected += [r for r in pending if r.get('draft_route') in ('human','review') and r['id'] not in known]
    sent = []
    for row in selected:
        allow_send, reasons = publish_gate.verdict(row)
        allow_send = allow_send and row.get('draft_route') != 'human'
        allow_override = publish_gate.can_override(reasons)
        if not (row.get('draft_text') or '').strip():
            row = dict(row, draft_text='Черновик не готов: нужны подтверждённые факты и/или LLM-контур. Ответьте через «Править».')
            allow_send = False
            allow_override = False
        markup = bot._kb(row['id'],allow_send=allow_send,allow_override=allow_override)
        if row.get('tg_msg_id') and str(row.get('tg_chat_id')) == str(bot.NOTIFY_IDS[0]):
            response = bot.edit_text(row['tg_chat_id'],row['tg_msg_id'],bot._card(row),reply_markup=markup)
            mid = row['tg_msg_id'] if (response or {}).get('ok') else None
        else:
            mid = bot.send(bot.NOTIFY_IDS[0],bot._card(row),reply_markup=markup)
        if mid:
            bot._set(row['id'],'carded',tg_chat_id=int(bot.NOTIFY_IDS[0]),tg_msg_id=mid,carded_at='now()')
            sent.append({'moderation_id':row['id'],'message_id':mid,'group':group(row)})
        time.sleep(0.7)
    return sent


def report():
    counts = db.query('''SELECT platform,account,kind,count(*) AS unanswered,
        count(*) FILTER(WHERE length(coalesce(draft_text,''))>0) AS drafts,
        count(*) FILTER(WHERE length(coalesce(draft_text,''))>0
            AND draft_route IN ('auto','review') AND coalesce(draft_grounding->>'fact_block','')=''
            AND (
            draft_grounding->>'verify'='PASS' OR
            draft_grounding->>'verification_method'='owner_approved_simple_review_20261111' OR
            (draft_grounding->>'verification_method'='source_rules_no_llm'
             AND draft_grounding->>'deterministic_verified'='true'))) AS ready_drafts,
        count(*) FILTER(WHERE skipped_old) AS historical_held,
        count(*) FILTER(WHERE NOT skipped_old) AS current_queue
        FROM raw_feedback WHERE NOT is_answered AND coalesce(recovery_active_unanswered,true)
        AND NOT skipped_no_api AND NOT coalesce(posted_ok,false) GROUP BY 1,2,3 ORDER BY 1,2,3''')
    queues = db.query('SELECT state,count(*) AS n FROM feedback_moderation GROUP BY state ORDER BY state')
    missing = [key for key in ['DEEPSEEK_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL'] if not os.getenv(key)]
    from reports import batch_health
    health = batch_health.status()
    llm_status = ('🔴 '+health['provider']['detail'] if health['provider'] else
                  'готовность конфигурации подтверждена; запросов в очереди: '+str(health['waiting_requests']))
    if health['uncertain_requests']:
        llm_status += '; результат части Batch требует сверки: '+str(health['uncertain_requests'])
    for failure in health.get('providers',[]):
        if failure['provider']=='deepseek' and failure['blocks_work']:
            llm_status += '\n🔴 '+failure['detail']
    if missing or os.getenv('FEEDBACK_LLM_PIPELINE_READY')!='1':
        llm_status = 'blocked: credentials/capabilities not validated'
    return {'at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'unanswered':counts,'moderation_states':queues,'missing_llm_configuration':missing,
            'llm_pipeline':llm_status, 'llm_health':health,
            'budget':recovery_budget.status(),'publication_block':runtime.publication_block(),
            'automatically_published':db.query("""SELECT count(*) AS n FROM raw_feedback f WHERE posted_ok=true
                AND (draft_route='auto' OR EXISTS(SELECT 1 FROM rev_archive_publication a
                    WHERE (a.platform,a.account,a.kind,a.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                    AND a.state='confirmed'))
                AND NOT EXISTS(SELECT 1 FROM rev_publication_attempt a
                WHERE (a.platform,a.account,a.kind,a.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                AND a.state='readback_missing')""")[0]['n'],
            'accepted_publications':db.query('SELECT count(*) AS n FROM raw_feedback WHERE posted_ok')[0]['n'],
            'publication_review':db.query("SELECT count(*) AS n FROM rev_publication_attempt WHERE state='readback_missing'")[0]['n'],
            'manually_published':db.query("""SELECT count(*) AS n FROM raw_feedback f WHERE posted_ok=true AND draft_route IS DISTINCT FROM 'auto'
                AND NOT EXISTS(SELECT 1 FROM rev_archive_publication a
                    WHERE (a.platform,a.account,a.kind,a.ext_id)=(f.platform,f.account,f.kind,f.ext_id))""")[0]['n'],
            'ozon_reviews':{'state':'separate blocked channel','count':None,'reason':'No API access/answer path; no fabricated zero or paid subscription'},
            'expert_memory':{t:db.query('SELECT count(*) AS n FROM '+t)[0]['n'] for t in ['brand_notes','approved_answers','compat_ref']}}


def send_status_if_due(result):
    """One daily summary, plus immediate provider/publication state changes."""
    path = OUT/'status-notification.json'
    now = time.time()
    provider = result['llm_health'].get('provider') or {}
    signature = json.dumps([result['publication_block'], provider.get('reason'),
                            bool(result['llm_health']['uncertain_requests']),
                            result['missing_llm_configuration']], sort_keys=True)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    if previous.get('signature')==signature and now-previous.get('sent_at',0)<86400:
        return False
    lines = ['Rev: '+('Публикации заблокированы: '+result['publication_block']
                     if result['publication_block'] else 'Автопубликация простых отзывов включена.')]
    lines += [f"{r['platform']}/{r['account']} {r['kind']}: без ответа {r['unanswered']}, "
              f"готовых {r['ready_drafts']}, архив {r['historical_held']}" for r in result['unanswered']]
    lines += [result['llm_pipeline'],
              f"LLM сегодня: расход ${result['budget']['spent_usd']}, резерв ${result['budget']['reserved_usd']}; предел $2."]
    if not bot.send(bot.NOTIFY_IDS[0], '\n'.join(lines)):
        return False
    path.write_text(json.dumps({'signature':signature,'sent_at':now}))
    return True


def main():
    os.umask(0o077)
    runtime.setup();recovery_budget.setup()
    for statement in ['CREATE INDEX IF NOT EXISTS idx_rev_raw_wb_nm ON raw_wb_card_content(nm_id)',
                      'CREATE INDEX IF NOT EXISTS idx_rev_wb_cards_nm ON wb_cards(nm_id)',
                      'CREATE INDEX IF NOT EXISTS idx_rev_ozon_product_sku ON ozon_product(sku)',
                      'CREATE INDEX IF NOT EXISTS idx_rev_ozon_attributes_offer ON raw_ozon_attributes(offer_id)']:
        db.execute(statement)
    collect.MAX_PAGES=100000;collect.LOOKBACK_DAYS=7;collect.KEEP_RECORDS=False
    if '--prepare-only' not in sys.argv:
        for account in ['wb_acc1','wb_acc2']:
            for kind in ['review','question']:
                collect.stage(account+':'+kind,lambda a=account,k=kind:collect.collect_wb(a,k))
        for account in ['oz_acc1','oz_acc2']:
            collect.stage(account+':question',lambda a=account:collect.collect_ozon(a))
        for kind in ['review','question']:
            collect.stage('ya_acc1:'+kind,lambda k=kind:collect.collect_yandex(k))
    active = refresh_wb_active()
    if '--prepare-only' not in sys.argv:
        refresh_catalog_if_stale()
        from collectors.rev_yandex_cards import refresh as refresh_native_market
        card_refresh = refresh_native_market()
        collect.report.append(dict(channel='ya_acc1:native_cards',
                                   status='error' if card_refresh['errors'] else 'ok', **card_refresh))
        if os.getenv('FEEDBACK_EVIDENCE_V2','0')=='1':
            from ops.rev_evidence_refresh import refresh as refresh_evidence_sources
            evidence_refresh=refresh_evidence_sources(max_pages=40)
            collect.report.append(dict(channel='technical_evidence',status='ok',details=evidence_refresh))
    with runtime.processing_lock() as wait_seconds:
        process_collected(active,wait_seconds)


def process_collected(active,wait_seconds=0):
    names_and_queue()
    from reports import compat_index
    compat_index.rebuild_if_stale(max_age_hours=24, verbose=False)
    subprocess.run([sys.executable,str(ROOT/'reports/recovery_drafts.py'),'--draft-only'],check=True)
    # The configured path is DeepSeek evidence -> Anthropic answer Batch -> controller Batch.
    # Missing credentials or unverified endpoint/pricing must never silently switch providers.
    if (os.getenv('FEEDBACK_LLM_PIPELINE_READY','0') == '1'
            and all(os.getenv(k) for k in ['DEEPSEEK_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL'])):
        from reports import llm_batch, feedback_today, recovery_review
        llm_batch.collect()
        from reports import batch_health
        if not batch_health.blocked():
            feedback_today.run(since='1970-01-01')
        recovery_review.tick()
        llm_batch.flush_if_due()
    queue_cases()
    publication = publication_phase()
    from collectors.feedback_preflight import verify_recent_publications,notify_readback_reviews
    source_checks=verify_recent_publications()
    notify_readback_reviews()
    queue_cases()
    delivered = send_cards(min(10, max(0, int(os.getenv('FEEDBACK_SAMPLE_LIMIT','10')))))
    result=report();result['delivered_this_cycle']=delivered;result['wb_active_snapshot']=active
    result['processing_lock_wait_seconds']=round(wait_seconds,3)
    result['collection_stages']=collect.report
    result['publication_phase']=publication
    result['publication_readback']=source_checks
    (OUT/'regular-cycle-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    send_status_if_due(result)
    print(json.dumps({'cycle':'completed','moderation_delivered':len(delivered),'publication_blocked':bool(result['publication_block'])}),flush=True)


if __name__ == '__main__':
    main()
