# поток: prc
"""Read-only live preflight and backup for an initial WB-name canary. No apply mode."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core import db, ms_api
from prices.catalog import model_dict, model_conflict
from prices.ms_import import ATTRS
from prices.wb_label import RULES_VERSION, fits_two_lines
from tools.prc.wb_fill import compose_details
from tools.prc.wb_name_audit import missing_fields, identity_issues

SEMANTIC_FIELDS = ('consumable_type', 'title', 'additional_title', 'printer_models',
                   'ink_colors', 'chip', 'capacity', 'volume_ml')


def digest(raw):
    return hashlib.sha256(json.dumps({k: raw.get(k) for k in SEMANTIC_FIELDS},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_live(row, product, raw, old_raw, model_index):
    reasons = []
    if product.get('id') != row['ms_id'] or product.get('externalCode') != row['external_code']:
        reasons.append('изменился id/внешний код')
    if product.get('code') != row['code'] or product.get('name') != row['supplier_name']:
        reasons.append('изменился код/товар поставщика')
    if product.get('archived'):
        reasons.append('карточка в архиве')
    attrs = {a['name']: a.get('value') for a in product.get('attributes', [])}
    current = str(attrs.get('Название WB') or '')
    if current.strip() != row['current']:
        reasons.append('изменилось прежнее Название WB')
    if raw is None or old_raw is None or digest(raw) != digest(old_raw):
        reasons.append('изменились/пропали исходные поля ТК')
    result = compose_details(raw, product.get('name') or '') if raw else None
    value = result.name if result else ''
    if not value or value != row['proposed'] or not fits_two_lines(value):
        reasons.append('новое название не совпадает с согласованным расчётом')
    if raw:
        reasons.extend(identity_issues(product, raw))
        if model_conflict(product.get('name') or '', row['external_code'], *model_index):
            reasons.append('противоречие модели поставщика и ТК')
    missing = missing_fields(product, value)
    if missing:
        reasons.append('нет обязательных полей: ' + ', '.join(missing))
    if attrs.get('Стоп лист') or re.search(r'брак|стоп.?лист|оригинал|\bOEM\b',
                                          product.get('pathName', '') + ' ' + (product.get('name') or ''), re.I):
        reasons.append('брак/стоп-лист/проверка оригинальности')
    return reasons, current, result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--limit', type=int, default=200)
    p.add_argument('--shortened-only', action='store_true')
    args = p.parse_args()
    if not 1 <= args.limit <= 200:
        p.error('initial canary limit must be 1..200')
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / 'plan.json').exists():
        p.error('existing plan is immutable; choose a new output directory')
    source_summary = json.loads((args.source / 'summary.json').read_text())
    for path, expected in source_summary['code_sha256'].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            p.error('source code changed since dry-run: ' + path)
    with gzip.open(args.source / 'review.csv.gz', 'rt', encoding='utf-8-sig') as f:
        selected = []
        for r in csv.DictReader(f):
            if r['status'] != 'NEEDS_TEMPLATE_AND_LIVE_CHECK' or (args.shortened_only and not r['actions']):
                continue
            selected.append(r)
            if len(selected) == args.limit:
                break
    with gzip.open(args.source / 'catalog_snapshot.json.gz', 'rt', encoding='utf-8') as f:
        old_catalog = json.load(f)
    codes = sorted({r['external_code'] for r in selected})
    models = db.query('SELECT external_code, raw FROM prc_tc_model WHERE gone_at IS NULL AND external_code = ANY(%s)',
                      (codes,))
    catalog = {r['external_code']: r['raw'] for r in models}
    model_index = model_dict()
    aid, _ = ATTRS['Доп. поле: Название WB']
    attribute = ms_api.get('/entity/product/metadata/attributes/' + aid)
    if attribute.get('name') != 'Название WB':
        raise RuntimeError('Target attribute identity mismatch')
    counts, plan, checks = Counter(), [], []
    with gzip.open(args.out / 'live_before.jsonl.gz', 'wt', encoding='utf-8') as backup:
        for start in range(0, len(selected), 20):
            batch = selected[start:start + 20]
            if any(r['entity'] != 'product' for r in batch):
                raise RuntimeError('First canary supports products only')
            ids = {r['ms_id'] for r in batch}
            response = ms_api.get('/entity/product', {'filter': ';'.join('id=' + r['ms_id'] for r in batch), 'limit': 20})
            products = {r['id']: r for r in response.get('rows', [])}
            if set(products) != ids or response.get('meta', {}).get('size') != len(ids):
                raise RuntimeError('Incomplete live batch; do not freeze a partial response')
            for row in batch:
                product = products[row['ms_id']]
                backup.write(json.dumps(product, ensure_ascii=False) + '\n')
                reasons, old, result = validate_live(row, product, catalog.get(row['external_code']),
                                                       old_catalog.get(row['external_code']), model_index)
                status = 'HOLD' if reasons else 'LIVE_CHECKED_PENDING_RELEASE_GATES'
                counts[status] += 1
                checks.append({'ms_id':row['ms_id'], 'code':row['code'], 'status':status, 'reasons':reasons})
                if not reasons:
                    plan.append({'ms_id':row['ms_id'], 'entity':'product', 'code':row['code'],
                                 'external_code':row['external_code'], 'article':product.get('article'),
                                 'supplier':product.get('supplier'), 'supplier_name':product.get('name'),
                                 'old':old, 'new':result.name, 'actions':list(result.actions),
                                 'tc_semantic_sha256':digest(catalog[row['external_code']]),
                                 'updated_at':product.get('updated')})
            time.sleep(0.5)
    for path, expected in source_summary['code_sha256'].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Code changed during preflight; discard plan')
    data = {'created_at_utc':datetime.now(timezone.utc).isoformat(), 'rules_version':RULES_VERSION,
            'scope':'first 200 shortened-name candidates; not the full catalogue',
            'source':str(args.source), 'source_code_sha256':source_summary['code_sha256'],
            'target_attribute_id':aid, 'items':plan, 'counts':dict(counts),
            'release_gates':{'original_template_or_print_checked':False,
                             'webhook_consumption_and_recovery_checked':False,
                             'backup_disk_space_for_execution_checked':False},
            'ready_to_apply':False, 'scheduled':False, 'ms_writes':0}
    encoded=json.dumps(data,ensure_ascii=False,indent=2).encode()
    (args.out / 'plan.json').write_bytes(encoded)
    (args.out / 'plan.sha256').write_text(hashlib.sha256(encoded).hexdigest()+'\n')
    (args.out / 'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    print(json.dumps({'selected':len(selected),'counts':dict(counts),'backup_saved':True,
                     'ready_to_apply':False,'ms_writes':0},ensure_ascii=False))


if __name__ == '__main__':
    main()
