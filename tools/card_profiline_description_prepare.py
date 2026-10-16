#!/usr/bin/env python3
# поток: card
"""Prepare Profiline description deltas for every active supplier card. GET only."""
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP
from tools.card_profiline_name_sample import key, norm, classes

OUT = ROOT / 'docs/reports/card_profiline_description_prepare_2026-10-09'
IDS = {sid for sid, group in ID2GROUP.items() if group == 'Профилайн'}


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def csv_save(name, rows):
    if rows:
        with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=';')
            w.writeheader(); w.writerows(rows)


def build(cards, offers):
    index = defaultdict(list)
    for offer in offers:
        if key(offer.get('vendorCode')):
            index[key(offer['vendorCode'])].append(offer)
    review = []
    for c in cards:
        if c.get('archived') or ms_api.meta_id(c, 'supplier') not in IDS:
            continue
        row = {'id': c['id'], 'external_code': c.get('externalCode') or '',
               'article': c.get('article') or '', 'supplier_id': ms_api.meta_id(c, 'supplier'),
               'ms_name': c.get('name') or '', 'old_description': c.get('description') or '',
               'description': '', 'source_article': '', 'source_code': '',
               'warehouse_stock': '', 'other_stock': '', 'old_chip': classes(c.get('description')),
               'source_chip': '', 'status': ''}
        found = index.get(key(c.get('article')), [])
        if not found:
            row['status'] = 'NO_SOURCE_ARTICLE'
        elif len(found) != 1:
            row['status'] = 'AMBIGUOUS_SOURCE_ARTICLE'
        else:
            o = found[0]
            row.update(description=o.get('name') or '', source_article=o['vendorCode'],
                       source_code=o.get('code') or '', warehouse_stock=o.get('warehouseStock', ''),
                       other_stock=o.get('quantity', ''), source_chip=classes(o.get('name')))
            if not isinstance(row['description'], str) or not row['description'].strip():
                row['status'] = 'EMPTY_SOURCE_NAME'
            elif norm(row['old_description']) == norm(row['description']):
                row['status'] = 'ALREADY_EQUAL_NORMALIZED'
            elif row['old_chip'] and row['old_chip'] != row['source_chip']:
                row['status'] = 'REVIEW_CHIP_INFORMATION_CHANGE'
            else:
                row['status'] = 'READY'
        review.append(row)
    return sorted(review, key=lambda x: x['id'])


def zero_stock(row):
    try:
        return float(row['warehouse_stock']) == 0 and float(row['other_stock']) == 0
    except (ValueError, TypeError):
        return False


def main():
    OUT.mkdir(exist_ok=True)
    token = os.environ.get('PROFILINE_API_TOKEN')
    if not token:
        raise RuntimeError('PROFILINE_API_TOKEN missing')
    try:
        r = requests.get('https://rm-profiline.online/json/hannover/client763/wms5/brand0/',
                         params={'token': token}, timeout=(15, 90))
    except requests.RequestException:
        raise RuntimeError('Profiline request failed') from None
    if r.status_code != 200:
        raise RuntimeError(f'Profiline HTTP {r.status_code}')
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError('Profiline response is not JSON') from None
    assert isinstance(data.get('products'), dict) and data['products']
    (OUT / 'catalog.json').write_text(r.text.replace(token, '[REDACTED]'))
    cards = {}
    for sid in sorted(IDS):
        for c in ms_api.iter_rows('/entity/product',
                                 {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=100):
            assert not c.get('archived') and ms_api.meta_id(c, 'supplier') in IDS
            cards[c['id']] = c
    save('ms_before.json', list(cards.values()))
    rows = build(cards.values(), data['products'].values())
    ready = [x for x in rows if x['status'] == 'READY']
    held = [x for x in rows if x['status'] == 'REVIEW_CHIP_INFORMATION_CHANGE']
    proposed = ready + held
    save('plan.json', ready)
    save('review.json', rows)
    save('held_chip.json', held)
    csv_save('plan.csv', ready)
    csv_save('all_description_deltas.csv', sorted(proposed, key=lambda x: x['id']))
    csv_save('held_chip.csv', held)
    csv_save('unmatched.csv', [x for x in rows if x['status'] in ('NO_SOURCE_ARTICLE', 'AMBIGUOUS_SOURCE_ARTICLE', 'EMPTY_SOURCE_NAME')])
    summary = {'at_utc': datetime.now(timezone.utc).isoformat(), 'source_date': data.get('date'),
               'source_sha256': hashlib.sha256(r.content).hexdigest(), 'source_items': len(data['products']),
               'active_supplier_cards': len(cards), 'statuses': dict(Counter(x['status'] for x in rows)),
               'ready_changes': len(ready), 'held_chip_changes': len(held), 'total_description_deltas': len(proposed),
               'ready_with_zero_supplier_stock': sum(zero_stock(x) for x in ready),
               'all_deltas_with_zero_supplier_stock': sum(zero_stock(x) for x in proposed),
               'stock_filter': False, 'archive_excluded': True, 'write_fields': ['description'],
               'writes': 0, 'database_writes': 0, 'status': 'prepared_only',
               'mapping': 'supplier group + unique vendorCode; case/whitespace normalization only'}
    assert len(rows) == len(cards) and len({x['id'] for x in ready}) == len(ready)
    assert all(norm(x['old_description']) != norm(x['description']) for x in ready)
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
