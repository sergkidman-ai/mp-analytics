"""Recover native product facts for unanswered cases and create unpaid drafts."""
import json
import os
from pathlib import Path
import sys
import time

import requests
from psycopg2.extras import Json

WORKTREE = Path(__file__).resolve().parents[1]
OUT = Path('/root/rev-recovery')
sys.path.insert(0, str(WORKTREE))
from core import db
from collectors import wb_card_content as wc, ozon_attributes as oa
from collectors.ozon import _headers, PRODUCT_INFO_URL
from reports import brand_notes
from reports.card_facts import CardFacts
from reports.feedback_draft_run import draft_review, draft_question, _first_name, _short

os.umask(0o077)
assert db.query('SELECT current_database() AS name')[0]['name'] == 'rev_recovery_20261103'
result = {'native_cards': [], 'llm_calls': 0, 'publications': 0}


def read(url, headers, body):
    r = requests.post(url, headers=headers, json=body, timeout=(8, 25))
    if not r.ok:
        raise RuntimeError('HTTP_' + str(r.status_code))
    return r.json()


def stage(label, callback):
    try:
        n = callback()
        entry = {'stage': label, 'status': 'ok', 'rows': n}
    except Exception as error:
        detail = str(error) if isinstance(error, RuntimeError) and str(error).startswith('HTTP_') else type(error).__name__
        entry = {'stage': label, 'status': 'failed', 'error': detail}
    result['native_cards'].append(entry)
    print(json.dumps(entry), flush=True)


def wb_cards(account):
    items = db.query("SELECT DISTINCT item_id FROM raw_feedback WHERE platform='wb' AND account=%s AND NOT is_answered", (account,))
    total = 0
    for row in items:
        data = read(wc.CARDS_URL, {'Authorization': wc._content_token(account)}, {
            'settings': {'cursor': {'limit': 20}, 'filter': {'withPhoto': -1, 'textSearch': row['item_id']}}})
        cards = [c for c in data.get('cards', []) if str(c.get('nmID')) == row['item_id']]
        total += wc.load_raw(account, cards)
        time.sleep(0.6)
    return total


def ozon_cards(account):
    items = db.query("SELECT DISTINCT item_id FROM raw_feedback WHERE platform='ozon' AND account=%s AND NOT is_answered", (account,))
    if not items:
        return 0
    skus = [r['item_id'] for r in items]
    headers = _headers(account)
    info = read(PRODUCT_INFO_URL, headers, {'sku': skus}).get('items') or []
    products = []
    for item in info:
        for sku in [item.get('sku')] + [s.get('sku') for s in item.get('sources', [])]:
            if sku:
                products.append({'account': account, 'sku': str(sku), 'offer_id': item.get('offer_id'),
                                 'name': item.get('name'), 'is_archived': bool(item.get('is_archived'))})
    if products:
        db.upsert('ozon_product', products, ['account', 'sku'], ['offer_id', 'name', 'is_archived'])
    pids = [str(i['id']) for i in info if i.get('id')]
    if not pids:
        return 0
    data = read(oa.ATTR_URL, headers, {'filter': {'product_id': pids, 'visibility': 'ALL'}, 'limit': 20, 'last_id': ''})
    attrs = data.get('result') or []
    if isinstance(attrs, dict):
        attrs = attrs.get('items') or []
    count = oa.load_raw(account, attrs)
    db.execute("""UPDATE raw_feedback f SET product_name=p.name, article=p.offer_id
        FROM ozon_product p WHERE f.platform='ozon' AND f.account=p.account
        AND f.item_id=p.sku AND f.account=%s AND NOT f.is_answered""", (account,))
    return count


if '--draft-only' not in sys.argv:
    for account in [r['account'] for r in db.query("SELECT DISTINCT account FROM raw_feedback WHERE platform='wb' AND NOT is_answered ORDER BY account")]:
        stage(account + ':native_cards', lambda a=account: wb_cards(a))
    for account in ['oz_acc1', 'oz_acc2']:
        stage(account + ':native_cards', lambda a=account: ozon_cards(a))

# Restore only the expert notes explicitly preserved in Git, not inferred history.
loaded, skipped = brand_notes.load_file(WORKTREE / 'docs/rev/brand_notes_2026-09-21.md',preserve_existing=True)
result['expert_notes_restored_from_git'] = loaded
loaded_rules, _ = brand_notes.load_file(WORKTREE / 'docs/rev/recovered_manual_rules_2026-11-04.md',preserve_existing=True)
result['manual_rules_restored_from_git'] = loaded_rules

# Correct timestamps from saved source payloads without re-fetching or losing state.
db.execute("""UPDATE raw_feedback SET created_at=(payload->>'published_at')::timestamptz,
    status=payload->>'status' WHERE platform='ozon' AND payload->>'published_at' IS NOT NULL""")
facts = CardFacts()
# Explicitly archived historical records remain visible in counts, without entering live drafts.
rows = db.query("""SELECT * FROM raw_feedback WHERE NOT is_answered
    AND coalesce(recovery_active_unanswered,true) AND NOT skipped_no_api AND NOT skipped_old
    AND NOT EXISTS (SELECT 1 FROM feedback_moderation m
        WHERE (m.platform,m.account,m.kind,m.ext_id)=(raw_feedback.platform,raw_feedback.account,raw_feedback.kind,raw_feedback.ext_id)
        AND (m.final_text IS NOT NULL OR m.decided_at IS NOT NULL
             OR EXISTS(SELECT 1 FROM rev_edit_session e WHERE e.mod_id=m.id)
             OR m.state IN ('carded','approved_blocked','sent','skipped','snoozed','edited')))
    ORDER BY platform,account,kind,ext_id""")
written = 0
for row in rows:
    if row['kind'] == 'review':
        category, text, route, confidence, template = draft_review(row, _first_name(row['payload']), _short(row.get('product_name') or ''))
        if category == 'negative' and any((row.get(k) or '').strip() for k in ['body', 'pros', 'cons']):
            route = 'review'  # partial recovery cannot establish a full product rating
        ground = {'source': 'шаблон', 'recovery': True, 'template_id': template}
        from reports.simple_review_policy import answer as simple_answer
        simple = simple_answer(row)
        if simple:
            _, text, route, confidence, ground, _, _ = simple
            template = ground['template_id']
    else:
        native = facts.for_ozon(row['item_id']) if row['platform'] == 'ozon' else facts.for_wb(row['item_id']) if row['platform'] == 'wb' else facts.for_yandex(row['item_id'])
        text, route, confidence, source = draft_question(row['body'], row.get('product_name'), native)
        if not native or not source.startswith('карточка'):
            text, route, confidence = '', 'human', 0
        ground = {'source': source, 'recovery': True, 'native_card_available': bool(native), 'verify': 'pending'}
        route = 'review' if text else 'human'
        ground['note'] = 'Первичная заготовка: ожидается штатная подготовка ответа и проверка контролёром.'
        category, template = 'question', 'recovery_native_card' if text else 'recovery_needs_human'
    db.execute("""UPDATE raw_feedback SET draft_text=%s,draft_route=%s,draft_confidence=%s,
        draft_category=%s,draft_grounding=%s,template_id=%s,draft_at=now()
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s AND NOT is_answered""",
        (text, route, confidence, category, Json(ground), template, row['platform'], row['account'], row['kind'], row['ext_id']))
    written += 1
result['cases_prepared'] = written
result['drafts'] = db.query("SELECT kind,draft_route,count(*) AS cases,count(*) FILTER(WHERE length(coalesce(draft_text,''))>0) AS nonempty FROM raw_feedback WHERE NOT is_answered GROUP BY 1,2 ORDER BY 1,2")
(OUT / 'draft-preparation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False), flush=True)
