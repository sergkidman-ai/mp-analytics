#!/usr/bin/env python3
# поток: card
"""Read-only current positive-stock Profiline audit; preserve previous write reports."""
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from core.db import query
from prices import loader, blacklist
from prices.profiles import get_identity
from tools.card_profiline_description_prepare import IDS
from tools.card_profiline_name_sample import key, norm, classes

OUT = ROOT / 'docs/reports/card_profiline_current_description_review_2026-10-12'


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def number(value):
    return float(str(value or '0').replace(',', '.'))


def positive(offer):
    return number(offer.get('warehouseStock')) > 0 or number(offer.get('quantity')) > 0


def collect():
    token = os.environ.get('PROFILINE_API_TOKEN')
    if not token:
        raise RuntimeError('Profiline token missing')
    try:
        response = requests.get('https://rm-profiline.online/json/hannover/client763/wms5/brand0/',
                                params={'token': token}, timeout=(15, 90))
    except requests.RequestException:
        raise RuntimeError('Profiline request failed') from None
    if response.status_code != 200:
        raise RuntimeError(f'Profiline HTTP {response.status_code}')
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError('Profiline invalid JSON') from None
    assert isinstance(data.get('products'), dict) and data['products']
    (OUT / 'catalog.json').write_text(response.text.replace(token, '[REDACTED]'))
    save('source_metadata.json', {'at_utc': datetime.now(timezone.utc).isoformat(),
                                'source_date': data.get('date'), 'offers': len(data['products'])})

    def fetch_supplier(sid):
        return list(ms_api.iter_rows('/entity/product',
            {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=100))
    with ThreadPoolExecutor(max_workers=2) as pool:
        cards = [c for rows in pool.map(fetch_supplier, sorted(IDS)) for c in rows]
    assert len({c['id'] for c in cards}) == len(cards)
    assert all(not c.get('archived') and ms_api.meta_id(c, 'supplier') in IDS for c in cards)
    save('ms_active_snapshot.json', cards)
    docs = loader.existing_docs(get_identity('profiline'))
    assert docs
    latest = max(d['name'].rsplit('_p', 1)[0] for d in docs)
    docs = [d for d in docs if d['name'].startswith(latest + '_p')]
    save('current_docs.json', docs)

    def fetch_positions(doc):
        data = ms_api.get('/entity/enter/' + doc['id'] + '/positions', {'limit': 1000})
        assert data['meta']['size'] <= 1000
        return {'id': doc['id'], 'name': doc['name'], 'positions': data['rows']}
    with ThreadPoolExecutor(max_workers=3) as pool:
        positions = list(pool.map(fetch_positions, docs))
    save('current_positions.json', positions)
    print(json.dumps({'collected_cards': len(cards), 'current_docs': len(docs)}, ensure_ascii=False), flush=True)


def analyse():
    data = json.loads((OUT / 'catalog.json').read_text())
    offers = list(data['products'].values())
    assert len({o['code'] for o in offers}) == len(offers)
    cards = json.loads((OUT / 'ms_active_snapshot.json').read_text())
    positions = json.loads((OUT / 'current_positions.json').read_text())
    entered = defaultdict(list)
    for d in positions:
        for p in d['positions']:
            entered[ms_api.meta_id(p, 'assortment')].append({'document': d['name'], 'quantity': p['quantity']})
    index = defaultdict(list)
    for o in offers:
        if key(o.get('vendorCode')):
            index[key(o['vendorCode'])].append(o)
    black = {r['article_norm']: r for r in query('SELECT * FROM prc_blacklist WHERE article_norm=ANY(%s)',
                         ([str(c.get('article') or '').strip().upper() for c in cards],))}
    save('relevant_blacklist.json', list(black.values()))
    review, outside, unresolved_enter = [], [], []
    for c in cards:
        matches = index.get(key(c.get('article')), [])
        positives = [o for o in matches if positive(o)]
        if not positives:
            outside.append({'id': c['id'], 'external_code': c.get('externalCode'), 'article': c.get('article'),
                            'reason': 'EXACT_SOURCE_ZERO_STOCK' if matches else 'NO_EXACT_CURRENT_SOURCE',
                            'entered': bool(entered[c['id']])})
            if entered[c['id']]:
                unresolved_enter.append({**outside[-1], 'ms_name': c.get('name'),
                                         'old_description': c.get('description'), 'positions': entered[c['id']]})
            continue
        exact = [o for o in matches if o.get('vendorCode') == c.get('article')]
        if len(exact) == 1:
            matches = exact
        row = {'id': c['id'], 'external_code': c.get('externalCode') or '', 'article': c.get('article') or '',
               'supplier_id': ms_api.meta_id(c, 'supplier'), 'ms_name': c.get('name') or '',
               'old_description': c.get('description') or '', 'source_code': '', 'source_name': '',
               'warehouse_stock': '', 'other_stock': '', 'category_id': '', 'entered': bool(entered[c['id']]),
               'status': '', 'old_chip': classes(c.get('description')), 'source_chip': '', 'candidates': []}
        if len(matches) != 1:
            row['status'] = 'AMBIGUOUS_SOURCE_ARTICLE'
            row['candidates'] = [{'code':o['code'], 'article':o.get('vendorCode'), 'name':o.get('name'),
                                 'warehouse_stock':o.get('warehouseStock'), 'other_stock':o.get('quantity')}
                                for o in matches]
        else:
            o = matches[0]
            if not positive(o):
                outside.append({'id':c['id'], 'external_code':c.get('externalCode'), 'article':c.get('article'),
                                'reason':'EXACT_ID_ZERO_STOCK_NORMALIZED_ALIAS_POSITIVE', 'entered':bool(entered[c['id']])})
                continue
            row.update(source_code=o['code'], source_name=norm(o.get('name')),
                       warehouse_stock=number(o.get('warehouseStock')), other_stock=number(o.get('quantity')),
                       category_id=o.get('categoryId'), source_chip=classes(o.get('name')))
            if 'брак' in row['ms_name'].lower():
                row['status'] = 'EXCLUDED_DEFECTIVE'
            elif row['article'].strip().upper() in black:
                row['status'] = 'EXCLUDED_BLACKLIST'
            elif re.search(r'^\s*(?:фото)?бумаг|^\s*пл[её]нк|^\s*термотрансфер', row['source_name'], re.I):
                row['status'] = 'EXCLUDED_KIND'
            elif re.search(r'\bоригинал(?:ьный|ьная|ьное|ьные)?\b|\boriginal\b', row['source_name'], re.I):
                row['status'] = 'EXCLUDED_ORIGINAL'
            elif blacklist.in_damaged(row['source_name']) or blacklist.in_kinds(row['source_name']):
                row['status'] = 'EXCLUDED_SUPPLIER_RULE'
            elif not row['source_name']:
                row['status'] = 'NO_SOURCE_NAME'
            elif norm(row['old_description']) == row['source_name']:
                row['status'] = 'ALREADY_EQUAL'
            else:
                row['status'] = 'DESCRIPTION_DIFFERENCE'
        review.append(row)
    grouped, ambiguous = defaultdict(list), []
    for r in review:
        if r['source_code']:
            grouped[r['source_code']].append(r)
        else:
            ambiguous.append(r)
    selected, shadowed = [], []
    for group in grouped.values():
        actual = [r for r in group if r['entered']]
        selected.extend(actual or group)
        if actual:
            shadowed.extend(r for r in group if not r['entered'])
    review = selected + ambiguous
    save('review.json', review)
    save('outside_current_positive_scope.json', outside)
    save('excluded_historical_aliases.json', shadowed)
    save('unresolved_current_receipt_cards.json', unresolved_enter)
    used = {r['source_code'] for r in review if r['source_code']}
    missing = [o for o in offers if positive(o) and o['code'] not in used]
    save('positive_source_without_selected_ms_card.json', missing)
    for filename, rows in [('all_current_positive_cards.csv',review),
                           ('description_differences.csv',[r for r in review if r['status']=='DESCRIPTION_DIFFERENCE']),
                           ('ambiguous_source_articles.csv',ambiguous)]:
        if rows:
            with (OUT/filename).open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader()
                w.writerows({**r,'candidates':json.dumps(r['candidates'],ensure_ascii=False)} for r in rows)
    summary = {'status':'read_only_complete', 'at_utc':datetime.now(timezone.utc).isoformat(),
               'source_date':data.get('date'), 'source_items':len(offers),
               'positive_moscow':sum(number(o.get('warehouseStock'))>0 for o in offers),
               'positive_other':sum(number(o.get('quantity'))>0 for o in offers),
               'positive_either':sum(positive(o) for o in offers),
               'active_supplier_cards':len(cards), 'selected_positive_cards':len(review),
               'statuses':dict(Counter(r['status'] for r in review)),
               'outside_scope':dict(Counter(r['reason'] for r in outside)),
               'current_receipt_documents':len(positions),
               'current_receipt_positions':sum(len(d['positions']) for d in positions),
               'selected_cards_in_receipts':sum(r['entered'] for r in review),
               'shadowed_historical_aliases':len(shadowed),
               'unresolved_current_receipt_cards':len(unresolved_enter),
               'positive_source_without_selected_card':len(missing),
               'missing_source_categories':dict(Counter(str(o.get('categoryId')) for o in missing)),
               'external_writes':0,'database_writes':0}
    save('summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    if '--saved' not in sys.argv:
        collect()
    analyse()
