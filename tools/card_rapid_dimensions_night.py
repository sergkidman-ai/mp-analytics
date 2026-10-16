#!/usr/bin/env python3
# поток: card
"""Prepare/apply the fixed authorized Rapid dimensions plan for October 14–15."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from prices import loader
from prices.profiles import get_identity
from tools.card_other_supplier_dimensions import key

SOURCE = ROOT / 'docs/reports/card_other_supplier_dimensions_2026-10-14'
ALL = ROOT / 'docs/reports/card_rapid_dimensions_all_2026-10-14'
OUT = ROOT / 'docs/reports/card_rapid_dimensions_night_2026-10-14'
START = datetime(2026, 10, 14, 20, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
WEBHOOK_ID = 'ed64d51f-6dfc-11f1-0a80-0e6f00770df1'
WEBHOOK_URL_SHA = '1948a8bd84aedfd9359c314807165f55f118a8518cb4bb5d56c95e702a762103'


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def log(**row):
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(), **row}, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def patch(live, row):
    if (live.get('id') != row['id'] or live.get('archived') or live.get('article') != row['article']
            or live.get('externalCode') != row['external_code']
            or ms_api.meta_id(live, 'supplier') != row['supplier_id']):
        raise RuntimeError('Product identity changed')
    if re.search(r'\bбрак\b', live.get('name',''), re.I) or any(
            'стоп' in a.get('name','').lower() and str(a.get('value')).strip().lower() in ('true','да','1')
            for a in live.get('attributes', [])):
        raise RuntimeError('Product is now marked damaged or stopped')
    old = live.get('attributes') or []
    targets = {a['id']: a for a in row['dimensions']}
    if len(targets) != 3:
        raise RuntimeError('Exactly three dimensions required')
    actual = {a['id']: a.get('value') for a in old if a.get('id') in targets}
    if len(actual) != len([a for a in old if a.get('id') in targets]):
        raise RuntimeError('Duplicate dimension attributes')
    missing = {}
    for aid, a in targets.items():
        value = actual.get(aid)
        if value is None or str(value).strip() == '':
            missing[aid] = a
        elif Decimal(str(value).replace(',', '.')) != Decimal(a['value']):
            raise RuntimeError('Dimension field is no longer empty and differs')
    if not missing:
        return None
    merged = [{**a, 'value': missing[a['id']]['value']} if a.get('id') in missing else a for a in old]
    present = {a.get('id') for a in old}
    merged.extend(a for aid, a in missing.items() if aid not in present)
    return {'id': row['id'], 'attributes': merged}


def prepare(all_rapid=False, authorized=False):
    if (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Existing execution journal')
    if (OUT/'authorization.json').exists():
        previous = json.loads((OUT/'authorization.json').read_text())
        if previous.get('authorized'):
            raise RuntimeError('Do not overwrite an authorized plan')
        for name in ['authorization.json','plan.json','summary.json','ms_before_prepare.json']:
            save('superseded_122/'+name, json.loads((OUT/name).read_text()))
    preview = json.loads(((ALL if all_rapid else SOURCE) / 'upload_fill_empty_preview.json').read_text())
    cards = {c['id']: c for c in json.loads((ALL / 'ms_active_snapshot.json').read_text())}
    metadata = json.loads((ROOT/'docs/reports/card_sprint_dimensions_prepare_2026-10-14/ms_attribute_metadata.json').read_text())['rows']
    axis_ids = [next(a['id'] for a in metadata if a['name'] == name)
                for name in ['Длина, см.', 'Ширина, см.', 'Высота, см.']]
    count = 1016 if all_rapid else 122
    assert len(preview) == len({r['id'] for r in preview}) == count
    plan = []
    for r in preview:
        c = cards[r['id']]
        targets = set(r['changed_attribute_ids'])
        assert targets == set(axis_ids)
        dimension_attrs = {a['id']: a for a in r['payload_preview']['attributes'] if a.get('id') in targets}
        row = {'id': c['id'], 'article': r['article'], 'external_code': r['external_code'],
               'supplier_id': ms_api.meta_id(c, 'supplier'), 'source_id': r['source_id'],
               'source_raw_m': r['source_raw'],
               'dimensions': [dimension_attrs[aid] for aid in axis_ids]}
        assert r['supplier'] == 'rapid' and key(row['article']) != 'SF-T9451BK'
        assert patch(c, row) is not None
        plan.append(row)
    plan.sort(key=lambda r: r['id'])
    save('plan.json', plan)
    save('ms_before_prepare.json', [cards[r['id']] for r in plan])
    digest = hashlib.sha256((OUT / 'plan.json').read_bytes()).hexdigest()
    save('authorization.json', {'authorized': authorized,
                                'scope': 'all_active_rapid' if all_rapid else 'current_receipts',
                                'instruction': 'давай сразу на 1016 прогон' if all_rapid else 'подготовь к ночной записи 122 позиции Рапида',
                                'preparation_only': not authorized, 'plan_sha256': digest,
                                'supplier_stock_filter': False if all_rapid else True,
                                'notification_mode': 'suppress_only_frozen_TC_product_UPDATE_per_batch',
                                'user_notified_in_conversation': True})
    save('summary.json', {'status': 'scheduled_ready' if authorized else 'prepared_not_scheduled', 'cards': count, 'fields': count*3,
                          'plan_sha256': digest, 'proposed_start_msk': '2026-10-14 23:20',
                          'cutoff_msk': '2026-10-15 05:00', 'batch_size': 50, 'pause_seconds': 60,
                          'canary_cards': min(200, count), 'canary_pause_seconds': 600,
                          'classification': 'silent per ms-mass-change: dimensions not read by TC; only TC subscribes to product UPDATE',
                          'webhook_suppression': 'only TC product UPDATE during each write batch; finally + ExecStopPost + 05:01 restore timer',
                          'writes': 0, 'verified': 0})
    print(f'Frozen {count} products / {count*3} fields; authorized={authorized}; no MS writes.', flush=True)


def checked_webhook():
    hook = ms_api.get('/entity/webhook/'+WEBHOOK_ID)
    if (hook.get('id') != WEBHOOK_ID or hook.get('entityType') != 'product'
            or hook.get('action') != 'UPDATE'
            or hashlib.sha256(hook.get('url','').encode()).hexdigest() != WEBHOOK_URL_SHA):
        raise RuntimeError('Frozen TC product UPDATE subscription changed')
    return hook


def set_webhook(enabled):
    # One attempt only. The recovery path GETs first to resolve uncertain results.
    try:
        r = requests.put(ms_api.BASE+'/entity/webhook/'+WEBHOOK_ID, json={'enabled':enabled},
                         headers={'Authorization':'Bearer '+ms_api.TOKEN}, timeout=(15,60))
    except requests.RequestException:
        raise RuntimeError('Uncertain webhook change: recovery GET required') from None
    if r.status_code != 200 or checked_webhook().get('enabled') is not enabled:
        raise RuntimeError('Webhook state verification failed')


def suppress_webhook():
    hooks = ms_api.get('/entity/webhook', {'limit':1000})
    if hooks['meta']['size'] > 1000:
        raise RuntimeError('Incomplete webhook inventory')
    active = [h for h in hooks['rows'] if h.get('enabled') and h.get('entityType')=='product' and h.get('action')=='UPDATE']
    if len(active) != 1 or active[0]['id'] != WEBHOOK_ID:
        raise RuntimeError('Product UPDATE consumer inventory changed')
    hook = checked_webhook()
    if hook.get('enabled') is not True:
        raise RuntimeError('TC webhook unexpectedly disabled before batch')
    save('webhook_restore_state.json', {'owned_change_pending':True, 'id':WEBHOOK_ID,
                                        'original_enabled':True, 'url_sha256':WEBHOOK_URL_SHA})
    set_webhook(False)
    log(phase='tc_product_update_suppressed', webhook_id=WEBHOOK_ID)


def restore_webhook():
    path = OUT/'webhook_restore_state.json'
    if not path.exists():
        return
    state = json.loads(path.read_text())
    if not state.get('owned_change_pending'):
        return
    if state['id'] != WEBHOOK_ID or state['url_sha256'] != WEBHOOK_URL_SHA or state['original_enabled'] is not True:
        raise RuntimeError('Unexpected restoration state')
    hook = checked_webhook()
    if hook.get('enabled') is not True:
        set_webhook(True)
    state['owned_change_pending'] = False
    state['restored_at_utc'] = datetime.now(timezone.utc).isoformat()
    save('webhook_restore_state.json',state)
    log(phase='tc_product_update_restored', webhook_id=WEBHOOK_ID)


def in_window():
    return START <= datetime.now(timezone.utc) < END


def guard():
    waited = False
    while True:
        if not in_window():
            raise RuntimeError('Outside fixed 23:20–05:00 MSK night')
        active = any(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
                     for unit in ['prc-enter-buyprice.service', 'card-solutionsprint-resolved-20261013.service',
                                  'card-supplier-ean13-20261013.service'])
        if not active:
            return waited
        waited = True
        time.sleep(30)


def operational_probe():
    guard()
    start = time.monotonic()
    ms_api.get('/entity/customerorder', {'limit': 1})
    if time.monotonic() - start > 10:
        raise RuntimeError('Operational MS read is slow')


def source_gate(plan, scope):
    token = os.environ['RAPID_API_AUTHKEY']
    try:
        response = requests.get('https://b2b-rapid1.ru/api/export.php', params={'authkey': token, 'type': 'json'}, timeout=(15,90))
    except requests.RequestException:
        raise RuntimeError('Fresh Rapid source unavailable') from None
    if response.status_code != 200:
        raise RuntimeError('Fresh Rapid source HTTP error')
    data = response.json()
    if data.get('error', {}).get('code') != 0:
        raise RuntimeError('Rapid source rejected')
    # No authenticated URL or token is written to disk.
    save('source_before_write.json', json.loads(json.dumps(data, ensure_ascii=False).replace(token, '[REDACTED]')))
    index = {}
    for o in data['price']:
        index.setdefault(key(o.get('CodeID')), {})[str(o['ID'])] = o
    entered = set()
    if scope == 'current_receipts':
        docs = loader.existing_docs(get_identity('rapid'))
        if not docs:
            raise RuntimeError('Current Rapid receipts missing')
        prefix = max(d['name'].rsplit('_p',1)[0] for d in docs)
        docs = [d for d in docs if d['name'].startswith(prefix+'_p')]
        for d in docs:
            positions = ms_api.get('/entity/enter/'+d['id']+'/positions', {'limit':1000})
            assert positions['meta']['size'] <= 1000
            entered.update(ms_api.meta_id(p, 'assortment') for p in positions['rows'])
        save('current_receipts_before_write.json', [{'id':d['id'],'name':d['name']} for d in docs])
    from prices import blacklist
    black = blacklist.load_set()
    eligible, skipped = [], []
    for row in plan:
        found = list(index.get(key(row['article']), {}).values())
        if len(found) > 1:
            found = [o for o in found if str(o.get('CodeID','')).strip() == row['article'].strip()]
        reason = ''
        if blacklist.norm(row['article']) in black:
            reason = 'BLACKLIST'
        elif scope == 'current_receipts' and row['id'] not in entered:
            reason = 'NO_LONGER_IN_CURRENT_RECEIPT'
        elif len(found) != 1 or str(found[0]['ID']) != row['source_id']:
            reason = 'SOURCE_IDENTITY_CHANGED'
        else:
            nums = [Decimal(str(found[0][k]).replace(',','.'))*100 for k in ['GabarityDlina','GabarityShirina','GabarityVisota']]
            expected = [Decimal(a['value']) for a in row['dimensions']]
            # Frozen dimension attributes retain the original D/W/H order.
            if nums != expected:
                reason = 'SOURCE_DIMENSIONS_CHANGED'
        if reason:
            skipped.append({'id':row['id'],'article':row['article'],'reason':reason})
        else:
            eligible.append(row)
    save('skipped_source_gate.json', skipped)
    return eligible, skipped


def run():
    auth = json.loads((OUT/'authorization.json').read_text())
    summary = json.loads((OUT/'summary.json').read_text())
    if auth.get('authorized') is not True or auth.get('notification_mode') != 'suppress_only_frozen_TC_product_UPDATE_per_batch':
        raise RuntimeError('Night execution not authorized / notification mode not recorded')
    if (OUT/'journal.jsonl').exists():
        raise RuntimeError('Existing journal: audit uncertain writes before any resume')
    digest = hashlib.sha256((OUT/'plan.json').read_bytes()).hexdigest()
    if digest != auth['plan_sha256'] or digest != summary['plan_sha256']:
        raise RuntimeError('Frozen plan changed')
    guard()
    plan, skipped = source_gate(json.loads((OUT/'plan.json').read_text()), auth['scope'])
    save('execution_plan.json', plan)
    summary.update(status='running', execution_cards=len(plan), source_skipped=len(skipped), already_desired=0)
    save('summary.json', summary)
    operational_probe()
    canary_done = False
    for start in range(0, len(plan), 50):
        guard()
        while True:
            before, payload, already = [], [], []
            for row in plan[start:start+50]:
                live = ms_api.get('/entity/product/'+row['id'])
                body = patch(live, row)
                if body:
                    before.append(live)
                    payload.append(body)
                else:
                    already.append(row['id'])
                time.sleep(.2)
            if not guard():
                break
        save(f'backup_batch_{start:04d}.json', before)
        save(f'payload_batch_{start:04d}.json', payload)
        for pid in already:
            log(phase='already_desired', id=pid)
            summary['already_desired'] += 1
        if payload:
            if not in_window():
                raise RuntimeError('Night window ended before POST')
            started = time.monotonic()
            try:
                suppress_webhook()
                if not in_window():
                    raise RuntimeError('Night window ended before POST')
                log(phase='sending', ids=[r['id'] for r in payload])
                try:
                    response = requests.post(ms_api.BASE+'/entity/product', json=payload,
                                             headers={'Authorization':'Bearer '+ms_api.TOKEN}, timeout=(15,120))
                except requests.RequestException:
                    raise RuntimeError('Uncertain write: no automatic retry') from None
            finally:
                restore_webhook()
            if response.status_code != 200:
                raise RuntimeError('MS POST failed; audit required')
            answer = response.json()
            if not isinstance(answer, list) or {r.get('id') for r in answer} != {r['id'] for r in payload}:
                raise RuntimeError('MS response IDs mismatch')
            elapsed = time.monotonic()-started
            summary['writes'] += len(payload)
            old_by_id = {c['id']:c for c in before}
            for body in payload:
                live = ms_api.get('/entity/product/'+body['id'])
                save('after/'+body['id']+'.json', live)
                if {a['id']:a.get('value') for a in live.get('attributes',[])} != {a['id']:a.get('value') for a in body['attributes']}:
                    raise RuntimeError('Attribute readback mismatch')
                for field in ['name','article','externalCode','code','description','supplier','barcodes','weight','volume','archived']:
                    if live.get(field) != old_by_id[body['id']].get(field):
                        raise RuntimeError('Other field changed during readback: '+field)
                log(phase='verified', id=body['id'])
                summary['verified'] += 1
                time.sleep(.2)
            save('summary.json', summary)
            if not canary_done and summary['verified'] >= min(200, len(plan)):
                for _ in range(20):
                    time.sleep(30)
                    guard()
                operational_probe()
                canary_done = True
            elif start+50 < len(plan):
                time.sleep(120 if elapsed > 10 else 60)
    if summary['verified']+summary['already_desired'] != len(plan):
        raise RuntimeError('Final count mismatch')
    summary.update(status='complete', completed_at_utc=datetime.now(timezone.utc).isoformat())
    save('summary.json', summary)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore-webhook', action='store_true')
    ap.add_argument('--all-rapid', action='store_true')
    ap.add_argument('--authorize-night', action='store_true')
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if args.restore_webhook:
        # Emergency recovery must also work if a timed-out writer still holds its lock.
        restore_webhook()
        raise SystemExit(0)
    with (OUT/'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            prepare(args.all_rapid, args.authorize_night)
        else:
            try:
                run()
            except Exception as exc:
                summary = json.loads((OUT/'summary.json').read_text())
                summary.update(status='stopped_requires_audit', error_type=type(exc).__name__)
                save('summary.json', summary)
                raise SystemExit(1)
