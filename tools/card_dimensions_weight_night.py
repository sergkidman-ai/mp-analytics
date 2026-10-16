#!/usr/bin/env python3
# поток: card
"""Authorized combined dimensions/weight night, 15–16 October; one touch per ID."""
import argparse
import csv
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from prices import blacklist
from tools import card_dimensions_own_source_night as previous
from tools.card_dimensions_source_inventory import sparse_xlsx

OUT = ROOT / 'docs/reports/card_dimensions_weight_night_2026-10-15'
AUDIT = ROOT / 'docs/reports/card_supplier_weight_audit_2026-10-15'
MANUAL_ID = '991d9628-420d-11f0-0a80-0919000f8c51'
MIN_FREE = 32 * 1024 * 1024
START, END = previous.START, previous.END


def save(name, data):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if name.endswith('.gz'):
        raw = gzip.compress(raw)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def log(**row):
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(), **row}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def load(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def number(value):
    try:
        d = Decimal(str(value if value not in (None, '') else 0).replace(',', '.'))
        if not d.is_finite() or d < 0:
            raise ValueError('Invalid weight')
        return Decimal(format(d, '.15g'))
    except InvalidOperation:
        raise ValueError('Invalid weight') from None


def check_disk():
    if shutil.disk_usage(OUT).free < MIN_FREE:
        raise RuntimeError('Insufficient disk space for durable backups/journal')


def guard():
    check_disk()
    return previous.guard()


def patch(live, row, black):
    if (live.get('id') != row['id'] or live.get('archived') or live.get('article') != row['article']
            or live.get('externalCode') != row['external_code'] or live.get('name') != row['name']
            or ms_api.meta_id(live, 'supplier') != row['supplier_id']):
        raise RuntimeError('IDENTITY_OR_NAME_DRIFT')
    if blacklist.norm(row['article']) in black or blacklist.in_damaged(live.get('name')) or re.search(r'\bбрак\b', live.get('name',''), re.I):
        raise RuntimeError('BLACKLIST_OR_DAMAGED')
    if any('стоп' in a.get('name','').lower() and str(a.get('value')).lower() in ('true','да','1') for a in live.get('attributes', [])):
        raise RuntimeError('STOP_LIST')
    body = previous.patch(live, row, black) if row.get('dimensions') else None
    body = body or {'id': row['id']}
    if row.get('weight_target') is not None:
        current, target = number(live.get('weight')), number(row['weight_target'])
        if current != target:
            if current != number(row['weight_before']):
                raise RuntimeError('WEIGHT_CHANGED_SINCE_APPROVED_AUDIT')
            body['weight'] = float(target)
    return body if len(body) > 1 else None


def fresh_rapid():
    token = os.environ['RAPID_API_AUTHKEY']
    try:
        response = requests.get('https://b2b-rapid1.ru/api/export.php', params={'authkey': token, 'type': 'json'}, timeout=(15,90))
        if response.status_code != 200:
            raise RuntimeError('Rapid HTTP failure')
        data = response.json()
        if data.get('error',{}).get('code') != 0:
            raise RuntimeError('Rapid API failure')
    except requests.RequestException:
        raise RuntimeError('Rapid read unavailable') from None
    save('rapid_source_latest.json.gz', json.loads(json.dumps(data, ensure_ascii=False).replace(token, '[REDACTED]')))
    return data


def check_sources(plan, hashes):
    previous.source_gate([r for r in plan if r.get('dimensions')], hashes)
    weight_index = defaultdict(set)
    for path in hashes:
        supplier = next((r['supplier'] for r in plan if r.get('source_file') == path), None)
        for sheet, rn, v in sparse_xlsx(ROOT / path):
            if rn == 1:
                continue
            mapping = None
            if supplier == 'Кактус':
                mapping = ('D', 'I')
            elif supplier == 'Сакура':
                mapping = {'Лазерная Sakura': ('E','AD'), 'Струйная Sakura': ('A','Y'),
                           'Матричная Sakura': ('A','S'), 'Наклейки SOK': ('A','Z')}.get(sheet)
            elif supplier == 'Изипринт':
                mapping = {'Данные по картриджам': ('A','J'), 'Чернила': ('B','I')}.get(sheet)
            if mapping and v.get(mapping[0]):
                try:
                    n = number(v.get(mapping[1]))
                    if n > 0:
                        weight_index[(supplier, previous.key(v[mapping[0]]))].add(n)
                except ValueError:
                    pass
    for item in fresh_rapid()['price']:
        try:
            n = number(item.get('Weight'))
            if n > 0:
                weight_index[('Рапид / Суперфайн', previous.key(item.get('CodeID')))].add(n)
        except ValueError:
            pass
    good, skipped = [], []
    for row in plan:
        target = row.get('weight_target')
        if row.get('manual_weight_override'):
            if row['id'] != MANUAL_ID or row['external_code'] != '6243' or row['article'] != 'I-B100_SET' or target != '0.48':
                raise RuntimeError('Manual override outside explicit user instruction')
        elif target is not None and weight_index[(row['supplier'], previous.key(row['article']))] != {number(target)}:
            skipped.append({'id': row['id'], 'reason': 'SOURCE_WEIGHT_DRIFT_OR_AMBIGUITY'})
            continue
        good.append(row)
    return good, skipped


def verify(after, body, before):
    expected_attrs = body.get('attributes', before.get('attributes', []))
    if {a['id']:a.get('value') for a in after.get('attributes',[])} != {a['id']:a.get('value') for a in expected_attrs}:
        raise RuntimeError('Attributes readback mismatch')
    if number(after.get('weight')) != number(body.get('weight', before.get('weight'))):
        raise RuntimeError('Weight readback mismatch')
    for field in ['name','article','externalCode','code','description','supplier','barcodes','volume','archived']:
        if after.get(field) != before.get(field):
            raise RuntimeError('Unrelated field changed: '+field)


def send(payload, prefix):
    check_disk()
    if not START <= datetime.now(timezone.utc) < END - timedelta(seconds=150):
        raise RuntimeError('Outside night window or too close to cutoff')
    log(phase='sending', ids=[p['id'] for p in payload])
    try:
        response = requests.post(ms_api.BASE+'/entity/product', json=payload,
            headers={'Authorization':'Bearer '+ms_api.TOKEN, previous.HEADER:prefix}, timeout=(15,120))
    except requests.RequestException:
        raise RuntimeError('Uncertain write: no automatic retry') from None
    if response.status_code != 200:
        raise RuntimeError('MS write rejected; audit required')
    answer = response.json()
    if not isinstance(answer,list) or len(answer) != len(payload) or {r.get('id') for r in answer} != {r['id'] for r in payload}:
        raise RuntimeError('Response ID mismatch; audit required')


def prepare():
    if (OUT/'authorization.json').exists() or (OUT/'journal.jsonl').exists():
        raise RuntimeError('Existing frozen plan: do not overwrite')
    check_disk()
    plan = {r['id']:r for r in load(previous.OUT/'plan.json')}
    if len(plan) != 364:
        raise RuntimeError('Wrong dimension scope')
    with (AUDIT/'all_active_weight_review.csv').open(encoding='utf-8-sig') as f:
        audit = list(csv.DictReader(f))
    rows = [r for r in audit if r['scope'] in ('PLANNED_DIMENSIONS_364','RAPID_DIMENSIONS_WRITTEN_1016') and r['decision'].startswith('CANDIDATE_')]
    if len(rows) != 579:
        raise RuntimeError('Wrong approved weight scope')
    manual = next(r for r in audit if r['ms_id'] == MANUAL_ID)
    rows.append(dict(manual, weight_source_numeric='0.48'))
    for r in rows:
        pid = r['ms_id']
        if pid not in plan:
            # Supplier identity is read from the saved fresh snapshot below.
            plan[pid] = {'id':pid, 'article':r['article'], 'external_code':r['external_code'],
                         'name':r['name'], 'supplier':r['supplier'], 'dimensions':[],
                         'weight_source_pointers':json.loads(r['source_pointers'])}
        plan[pid].update(weight_target=r['weight_source_numeric'], weight_before=r['weight_ms'] or '0')
        if pid == MANUAL_ID:
            plan[pid]['manual_weight_override'] = True
    previous_cards = {r['id']:r for r in json.loads(gzip.decompress((AUDIT/'ms_live_1380.json.gz').read_bytes()))}
    manual_live = ms_api.get('/entity/product/'+MANUAL_ID)
    previous_cards[MANUAL_ID] = manual_live
    for row in plan.values():
        row['supplier_id'] = row.get('supplier_id') or ms_api.meta_id(previous_cards[row['id']], 'supplier')
    plan = sorted(plan.values(), key=lambda r:r['id'])
    hashes = load(previous.PROPOSAL/'proposal_integrity.json')['source_sha256']
    eligible, source_skips = check_sources(plan, hashes)
    if source_skips:
        save('prepare_source_skips.json',source_skips)
        raise RuntimeError('Fresh source differs: review before scheduling')
    previous.checked_prefix()
    black = blacklist.load_set()
    ids = [r['id'] for r in plan]
    def batch_read(chunk):
        data = ms_api.get('/entity/product', {'filter':';'.join('id='+i for i in chunk),'limit':100})
        if data['meta']['size'] != len(chunk) or {p['id'] for p in data['rows']} != set(chunk):
            raise RuntimeError('Fresh MS collection mismatch')
        time.sleep(.2)
        return data['rows']
    with ThreadPoolExecutor(max_workers=2) as pool:
        fresh = {p['id']:p for chunk in pool.map(batch_read,[ids[i:i+40] for i in range(0,len(ids),40)]) for p in chunk}
    checks = []
    for row in plan:
        try:
            body = patch(fresh[row['id']],row,black)
            checks.append({'id':row['id'],'status':'ready' if body else 'already_desired'})
        except (RuntimeError,ValueError) as exc:
            checks.append({'id':row['id'],'status':'skip','reason':str(exc)})
    save('preflight.json',checks)
    save('ms_preflight.json.gz',list(fresh.values()))
    if any(r['status']=='skip' for r in checks):
        raise RuntimeError('Fresh MS drift: review before scheduling')
    if len(plan) != 605 or sum(bool(r.get('dimensions')) for r in plan) != 364 or sum(r.get('weight_target') is not None for r in plan) != 580:
        raise RuntimeError('Scope mismatch')
    save('plan.json',plan)
    sha = digest(OUT/'plan.json')
    save('authorization.json',{'authorized':True,'instruction':'6243 поставь вес 0,48 кг. Обновление веса включил в ночную запись?',
         'scope':'364 agreed dimensions + their 339 weights + 240 weights of prior Rapid 1016 + explicit EasyPrint I-B100_SET 6243 override',
         'plan_sha256':sha,'source_sha256':hashes,'notification_mode':previous.MODE,
         'accept_gross_or_net':True,'confirmed_weight_units':'kg','stock_filter':False,
         'cross_supplier_transfer':False,'manual_override':{'id':MANUAL_ID,'article':'I-B100_SET','weight':0.48},
         'user_notified_in_conversation':True,'external_TC_polling_recovery_verified':False})
    save('summary.json',{'status':'authorized_ready_not_scheduled','cards':605,'dimension_cards':364,'weight_cards':580,
         'dimension_fields':1092,'total_changed_fields':1672,'plan_sha256':sha,'writes':0,'verified':0,
         'start_msk':'2026-10-15 23:20','cutoff_msk':'2026-10-16 05:00','min_free_bytes':MIN_FREE,
         'preflight':dict(Counter(r['status'] for r in checks)),'skipped':0,'already_desired':0})
    print(json.dumps({'cards':605,'dimension_cards':364,'weight_cards':580,'preflight':Counter(r['status'] for r in checks),'MS_writes':0}))


def apply():
    auth, summary = load(OUT/'authorization.json'), load(OUT/'summary.json')
    if auth.get('authorized') is not True or auth.get('notification_mode') != previous.MODE:
        raise RuntimeError('Not authorized')
    if (OUT/'journal.jsonl').exists():
        raise RuntimeError('Existing journal: audit before any resume')
    if digest(OUT/'plan.json') != auth['plan_sha256'] or auth['plan_sha256'] != summary['plan_sha256']:
        raise RuntimeError('Frozen plan changed')
    guard()
    plan, skipped = check_sources(load(OUT/'plan.json'),auth['source_sha256'])
    save('source_skipped.json',skipped)
    summary.update(status='running',source_skipped=len(skipped))
    save('summary.json',summary)
    previous.checked_prefix(); previous.probe()
    for start in range(0,len(plan),50):
        guard()
        while True:
            black=blacklist.load_set(); before={}; payload=[]; drift=[]; already=[]
            for row in plan[start:start+50]:
                live=ms_api.get('/entity/product/'+row['id'])
                try:
                    body=patch(live,row,black)
                except (RuntimeError,ValueError) as exc:
                    drift.append({'id':row['id'],'reason':str(exc)}); continue
                if body:
                    before[row['id']]=live; payload.append(body)
                else:
                    already.append(row['id'])
                time.sleep(.2)
            if not guard():
                break
        save(f'backup_batch_{start:04}.json.gz',list(before.values()))
        save(f'payload_batch_{start:04}.json.gz',payload)
        for row in drift: log(phase='skipped',**row)
        for pid in already: log(phase='already_desired',id=pid)
        summary['skipped']+=len(drift); summary['already_desired']+=len(already)
        elapsed=0
        if payload:
            prefix=previous.checked_prefix(); started=time.monotonic()
            send(payload,prefix); elapsed=time.monotonic()-started
            summary['writes']+=len(payload); save('summary.json',summary)
            for body in payload:
                live=ms_api.get('/entity/product/'+body['id'])
                save('after/'+body['id']+'.json.gz',live)
                verify(live,body,before[body['id']])
                log(phase='verified',id=body['id']); summary['verified']+=1
                time.sleep(.2)
            previous.checked_prefix()
        save('summary.json',summary)
        if start == 150:
            log(phase='canary_pause_start',verified=summary['verified'])
            for _ in range(20): time.sleep(30); guard()
            previous.probe(); log(phase='canary_health_passed')
        elif start+50<len(plan):
            time.sleep(120 if elapsed>10 else 60)
    if summary['verified']+summary['skipped']+summary['already_desired'] != len(plan):
        raise RuntimeError('Final count mismatch')
    summary.update(status='complete',completed_at_utc=datetime.now(timezone.utc).isoformat())
    save('summary.json',summary)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',action='store_true'); mode.add_argument('--apply',action='store_true'); args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    with (OUT/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            prepare() if args.prepare else apply()
        except Exception as exc:
            if args.apply and (OUT/'summary.json').exists():
                summary=load(OUT/'summary.json')
                if summary.get('status')!='complete':
                    summary.update(status='stopped_requires_audit',error_type=type(exc).__name__)
                    save('summary.json',summary)
            print('Stopped safely: '+type(exc).__name__,flush=True)
            raise SystemExit(1)
