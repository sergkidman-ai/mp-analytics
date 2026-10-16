#!/usr/bin/env python3
# поток: card
"""Freeze and apply owner-resolved Bulat descriptions during the MS night window."""
import argparse
import csv
import fcntl
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from tools.card_bulat_description_audit import normalized
from tools.card_rapid_description_sync import apply, checked_write_payload

SOURCE = ROOT / 'docs/reports/card_bulat_current_description_review_2026-10-12'
OUT = ROOT / 'docs/reports/card_bulat_resolved_description_sync_2026-10-13'
MSK = ZoneInfo('Europe/Moscow')
FEED_URL = 'https://bulat-group.ru/price/clientapi.xml'


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def feed():
    response = requests.get(FEED_URL, timeout=(15, 90))
    response.raise_for_status()
    raw = response.content
    root = ET.fromstring(raw)
    assert root.tag == 'yml_catalog'
    return raw, {o.get('id'): o for o in root.findall('.//offer')}


def targets():
    with (SOURCE / 'expanded_description_targets.csv').open(encoding='utf-8-sig') as f:
        first = list(csv.DictReader(f, delimiter=';'))
    with (SOURCE / 'remaining_20_short_name_review.csv').open(encoding='utf-8-sig') as f:
        remaining = list(csv.DictReader(f, delimiter=';'))
    assert len(first) == 146 and len(remaining) == 20
    selected = []
    for row in remaining:
        if row['review_status'] == 'NO_SHORT_NAME':
            continue
        assert normalized(row['short_name'])
        target = normalized(row['short_name'])
        if row['external_code'] == '1727':
            # The supplier's complete description explicitly retains this limit.
            assert 'до версии LW73 включительно' in row['full_description']
            target += ', до версии LW73 включительно'
        selected.append({'external_code': row['external_code'], 'article': row['article'],
                         'id': row['id'], 'stock': row['stock'], 'entered': True,
                         'old_description': row['old_description'],
                         'target_description': target, 'basis': 'OWNER_SUPPLIER_SHORT_NAME'})
    assert len(selected) == 19
    result = first + selected
    assert len(result) == 165 and len({r['id'] for r in result}) == 165
    return result


def prepare():
    frozen = targets()
    snapshot = {r['id']: r for r in json.loads((SOURCE / 'review.json').read_text())}
    raw, offers = feed()
    plan, live_before = [], []
    for r in frozen:
        base = snapshot[r['id']]
        assert r['article'] == base['article'] == base['source_id']
        assert r['external_code'] == base['external_code']
        assert r['old_description'] == base['old_description']
        offer = offers.get(r['article'])
        assert offer is not None and float((offer.findtext('count') or '0').replace(',', '.')) > 0
        assert normalized(offer.findtext('name')) == normalized(base['source_name'])
        if r['basis'] in ('SHORT_NAME_FALLBACK', 'OWNER_EXPLICIT_SHORT_NAME',
                          'OWNER_SUPPLIER_SHORT_NAME') and r['external_code'] != '1727':
            assert normalized(r['target_description']) == normalized(offer.findtext('name'))
        live = ms_api.get('/entity/product/' + r['id'])
        row = {'id': r['id'], 'external_code': r['external_code'], 'article': r['article'],
               'supplier_id': base['supplier_id'], 'old_description': r['old_description'],
               'description': r['target_description'], 'feed_offer_id': offer.get('id'),
               'api_free_stock': float((offer.findtext('count') or '0').replace(',', '.')),
               'basis': r['basis']}
        patch = checked_write_payload(live, row)
        if patch:
            plan.append(row)
            live_before.append(live)
    assert len(plan) == 165
    plan.sort(key=lambda r: r['id'])
    save('plan.json', plan)
    save('ms_before.json', live_before)
    with (OUT / 'plan.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(plan[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(plan)
    summary = {'status': 'prepared', 'at': datetime.now(MSK).isoformat(),
               'source_feed_date': ET.fromstring(raw).get('date'),
               'feed_sha256': hashlib.sha256(raw).hexdigest(), 'changes': len(plan),
               'write_fields': ['description'], 'max_batch': 50, 'min_batch_pause_seconds': 60,
               'night_window_msk': '23:00-05:00', 'writes': 0, 'verified': 0}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def run():
    summary = json.loads((OUT / 'summary.json').read_text())
    if summary['status'] != 'prepared':
        raise RuntimeError('Plan is not in prepared state')
    plan = json.loads((OUT / 'plan.json').read_text())
    if len(plan) != summary['changes'] or len({r['id'] for r in plan}) != len(plan):
        raise RuntimeError('Frozen plan invalid')
    raw, offers = feed()
    if hashlib.sha256(raw).hexdigest() != summary['feed_sha256']:
        raise RuntimeError('Supplier feed changed after preparation; stop before writes')
    for row in plan:
        offer = offers.get(row['feed_offer_id'])
        if offer is None or float((offer.findtext('count') or '0').replace(',', '.')) <= 0:
            raise RuntimeError('Source offer no longer stock-positive')
    apply(OUT, plan, summary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.prepare == args.apply:
        parser.error('Specify exactly one of --prepare and --apply')
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            if (OUT / 'journal.jsonl').exists():
                raise RuntimeError('Write journal exists; do not replace frozen plan')
            prepare()
        else:
            run()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if OUT.joinpath('summary.json').exists():
            summary = json.loads(OUT.joinpath('summary.json').read_text())
            summary.update({'status': 'failed', 'error_type': type(exc).__name__})
            save('summary.json', summary)
        raise
