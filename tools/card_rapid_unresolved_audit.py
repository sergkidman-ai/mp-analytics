#!/usr/bin/env python3
# поток: card
"""Read-only audit of the frozen 1252 Rapid entities and their current alternatives."""
import json
import csv
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from core.db import query
from invoice_bot.supplier_groups import ID2GROUP
from prices import loader
from prices.profiles import get_identity
from tools.card_rapid_description_sync import source, save, stamp

BASE = ROOT / 'docs/reports/card_rapid_unmatched_stock_2026-10-12'
OUT = ROOT / 'docs/reports/card_rapid_unresolved_1252_2026-10-12'
SUPPLIERS = {sid for sid, group in ID2GROUP.items() if group == 'Суперфайн'}


def key(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def full_name(value):
    return re.sub(r'\W', '', str(value or '').casefold().replace('ё', 'е'))


def analyze():
    """Model availability is separate from identity of a historical supplier SKU."""
    read = lambda name: json.loads((OUT / name).read_text())
    scope = read('scope.json')
    cards = {c['id']: c for c in read('ms_scope_current.json')}
    alternatives = read('ms_same_code_current.json') + read('ms_tc_linked_codes_current.json')
    items = read('catalogue.json')['price']
    black = {r['article_norm']: r for r in read('blacklist.json')}
    nov = defaultdict(list)
    for row in read('novelty.json'):
        nov[key(row['article'])].append(row)
    articles, names, codes, links, positions = (defaultdict(list) for _ in range(5))
    for i, row in enumerate(items):
        if row.get('CodeID'):
            articles[key(row['CodeID'])].append(i)
        if row.get('Name'):
            names[full_name(row['Name'])].append(i)
    for c in {c['id']: c for c in alternatives}.values():
        codes[c.get('externalCode', '')].append(c)
    for row in read('tc_links.json'):
        # One explicit hop only; never infer equivalence from a transitive chain.
        links[row['external_code']].append(row['ref_code'])
        links[row['ref_code']].append(row['external_code'])
    for doc in read('current_enter_positions.json'):
        for pos in doc['positions']:
            positions[ms_api.meta_id(pos, 'assortment')].append({
                'document': doc['name'], 'quantity': pos['quantity'], 'price_rub': pos['price']/100})
    model_candidates = {r['id']: r for r in read('model_candidate_review.json')}
    reviewed = []
    for frozen in scope:
        c = cards.get(frozen['id'])
        if not c:
            reviewed.append({**frozen, 'availability': 'MS_CARD_MISSING', 'mapping': 'NONE'})
            continue
        direct = articles.get(key(c.get('article')), []) if c.get('article') else []
        text_matches = set()
        for field in ('description', 'name'):
            if c.get(field):
                text_matches.update(names.get(full_name(c[field]), []))
        direct = set(direct) if len(direct) == 1 else text_matches if len(text_matches) == 1 else set()
        related, same, refs = set(), set(), defaultdict(list)
        for method, allowed in [('SAME_TC_CODE', {c.get('externalCode', '')}),
                                ('EXPLICIT_TC_LINK', set(links[c.get('externalCode', '')]))]:
            for code in allowed:
                for alt in codes[code]:
                    found = articles.get(key(alt.get('article')), []) if alt.get('article') else []
                    if len(found) == 1:
                        i = found[0]
                        (same if method == 'SAME_TC_CODE' else related).add(i)
                        refs[i].append({'id': alt['id'], 'article': alt.get('article'),
                                        'external_code': code, 'mapping': method,
                                        'positions': positions.get(alt['id'], [])})
        positive = lambda indices: {i for i in indices if float(items[i]['UnitsFree']) > 0}
        if direct:
            selected, method = direct, 'UNIQUE_FULL_NAME_OR_ARTICLE'
        elif positive(same):
            selected, method = same, 'SAME_TC_CODE'
        elif positive(related):
            selected, method = related, 'EXPLICIT_TC_LINK'
        elif same or related:
            selected, method = same | related, 'KNOWN_TC_VARIANTS'
        else:
            selected, method = set(), 'NONE'
        good = positive(selected)
        candidate = model_candidates[c['id']]
        candidate_positive = [r for r in candidate['candidates'] if float(r['stock']) > 0]
        state = ('MODEL_IN_STOCK' if good else 'KNOWN_VARIANTS_ZERO_FREE' if selected else
                 'SIMILAR_POSITIVE_NEEDS_REVIEW' if candidate_positive else
                 'SIMILAR_ZERO_NEEDS_REVIEW' if candidate['candidates'] else 'NO_CURRENT_SOURCE_MATCH')
        text = ' '.join(str(c.get(k) or '') for k in ('name', 'description'))
        original = bool(re.search(r'оригинал|\boriginal\b', text, re.I)
                        or re.search(r'(^OR[- ]|[ -]OR$)', c.get('article') or '', re.I)
                        or re.search(r'[-]?or$', c.get('code') or '', re.I))
        excluded_kind = bool(re.search(r'^\s*(?:фото)?бумаг|^\s*пл[её]нк|^\s*термотрансфер',
                                       c.get('name') or '', re.I))
        source_rows = []
        for i in sorted(selected):
            r = items[i]
            source_rows.append({'source_id': r['ID'], 'article': r['CodeID'], 'name': r['Name'],
                                'units_free': r['UnitsFree'], 'vendor': r.get('Vendor'),
                                'ms_cards': refs[i], 'blacklisted': r['CodeID'].strip().upper() in black,
                                'novelty': [{'id': n['id'], 'decision': n['decision'], 'ms_id': n.get('ms_id')}
                                            for n in nov[key(r['CodeID'])]]})
        for r in candidate['candidates']:
            r['blacklisted'] = r['article'].strip().upper() in black
            r['novelty'] = [{'id': n['id'], 'decision': n['decision'], 'ms_id': n.get('ms_id'),
                             'present_in_current_enter': n.get('ms_id') in positions}
                            for n in nov[key(r['article'])]]
        reviewed.append({'id': c['id'], 'external_code': c.get('externalCode'), 'ms_code': c.get('code'),
                         'article': c.get('article'), 'name': c.get('name'), 'description': c.get('description'),
                         'availability': state, 'mapping': method, 'archived': bool(c.get('archived')),
                         'original_excluded': original, 'excluded_kind': excluded_kind,
                         'old_article_blacklisted': str(c.get('article') or '').strip().upper() in black,
                         'original_card_in_enter': bool(positions.get(c['id'])),
                         'positive_source_count': len(good), 'source_rows': source_rows,
                         'positive_source_already_in_enter': any(ref['positions'] for i in good for ref in refs[i]),
                         'source_candidates': candidate['candidates']})
    assert len(reviewed) == len({r['id'] for r in reviewed}) == 1252
    save(OUT / 'review.json', reviewed)
    columns = ['id', 'external_code', 'article', 'name', 'availability', 'mapping',
               'original_excluded', 'excluded_kind', 'old_article_blacklisted',
               'positive_source_already_in_enter', 'source_rows', 'source_candidates']
    groups = {'all_1252.csv': reviewed,
              'models_in_stock.csv': [r for r in reviewed if r['availability'] == 'MODEL_IN_STOCK'],
              'known_variants_zero_free.csv': [r for r in reviewed if r['availability'] == 'KNOWN_VARIANTS_ZERO_FREE'],
              'positive_candidates_for_review.csv': [r for r in reviewed if r['availability'] == 'SIMILAR_POSITIVE_NEEDS_REVIEW'],
              'no_current_source_match.csv': [r for r in reviewed if r['availability'] == 'NO_CURRENT_SOURCE_MATCH']}
    for name, group in groups.items():
        with (OUT / name).open('w', encoding='utf-8-sig', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=columns, delimiter=';', extrasaction='ignore')
            w.writeheader()
            for r in group:
                w.writerow({**r, 'source_rows': json.dumps(r.get('source_rows', []), ensure_ascii=False),
                            'source_candidates': json.dumps(r.get('source_candidates', []), ensure_ascii=False)})
    stock_rows = groups['models_in_stock.csv']
    summary = {'status': 'complete_read_only', 'at': stamp(), 'scope': 1252,
               'source': read('source_metadata.json'),
               'availability_counts': dict(Counter(r['availability'] for r in reviewed)),
               'mapping_counts': dict(Counter(r['mapping'] for r in reviewed)),
               'in_stock_historical_cards': len(stock_rows),
               'in_stock_unique_external_codes': len({r['external_code'] for r in stock_rows}),
               'in_stock_already_in_current_enter': sum(r['positive_source_already_in_enter'] for r in stock_rows),
               'explicit_originals_excluded': sum(r.get('original_excluded', False) for r in reviewed),
               'original_card_still_in_enter': sum(r.get('original_card_in_enter', False) for r in reviewed),
               'external_writes': 0, 'database_writes': 0,
               'note': 'Stock by active supplier cards or direct TC links is model availability, '
                       'not proof the old article remains sold. Candidate-only matches and missing '
                       'articles never establish zero stock; do not sum stock across historical duplicates.'}
    save(OUT / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


def collect():
    OUT.mkdir(exist_ok=True)
    scope = [r for r in json.loads((BASE / 'review.json').read_text()) if r['status'] == 'UNRESOLVED']
    assert len(scope) == len({r['id'] for r in scope}) == 1252
    save(OUT / 'scope.json', scope)
    data = source(OUT)
    save(OUT / 'source_metadata.json', {'fetched_at': stamp(), 'source_date': data.get('date'),
                                      'source_items': len(data['price'])})

    def read_ids(batch):
        params = {'filter': [*(f'id={i}' for i in batch), 'archived=true', 'archived=false'], 'limit': 20}
        d = ms_api.get('/entity/product', params)
        assert d['meta']['size'] <= 20 and {r['id'] for r in d['rows']} <= set(batch)
        return d['rows']

    ids = [r['id'] for r in scope]
    with ThreadPoolExecutor(max_workers=3) as pool:
        cards = [r for batch in pool.map(read_ids, [ids[i:i+20] for i in range(0, len(ids), 20)]) for r in batch]
    save(OUT / 'ms_scope_current.json', cards)
    assert all(ms_api.meta_id(c, 'supplier') in SUPPLIERS for c in cards if not c.get('archived'))
    codes = sorted({c['externalCode'] for c in cards if c.get('externalCode') and not c.get('archived')})

    def read_codes(batch):
        params = {'filter': [*(f'externalCode={c}' for c in batch),
                             *(f'supplier={ms_api.BASE}/entity/counterparty/{s}' for s in SUPPLIERS),
                             'archived=false']}
        return list(ms_api.iter_rows('/entity/product', params, page=100))

    with ThreadPoolExecutor(max_workers=3) as pool:
        alternatives = [r for batch in pool.map(read_codes, [codes[i:i+10] for i in range(0, len(codes), 10)])
                        for r in batch]
    save(OUT / 'ms_same_code_current.json', alternatives)
    docs = loader.existing_docs(get_identity('rapid'))
    latest = max(d['name'].rsplit('_p', 1)[0] for d in docs)
    docs = [d for d in docs if d['name'].startswith(latest + '_p')]

    def read_doc(doc):
        d = ms_api.get('/entity/enter/' + doc['id'] + '/positions', {'limit': 1000})
        assert d['meta']['size'] <= 1000
        return {'id': doc['id'], 'name': doc['name'], 'positions': d['rows']}

    with ThreadPoolExecutor(max_workers=3) as pool:
        positions = list(pool.map(read_doc, docs))
    save(OUT / 'current_enter_positions.json', positions)
    relevant_articles = sorted({str(c.get('article') or '').strip().upper() for c in cards + alternatives}
                               | {str(r.get('CodeID') or '').strip().upper() for r in data['price']})
    black = query('SELECT article_norm,article,source,note,added_at FROM prc_blacklist '
                  'WHERE article_norm=ANY(%s)', (relevant_articles,))
    save(OUT / 'blacklist.json', json.loads(json.dumps(black, default=str)))
    novelty = query("SELECT * FROM prc_novelty WHERE supplier_key='rapid' AND "
                    '(article_norm=ANY(%s) OR ms_id=ANY(%s))', (relevant_articles, ids))
    save(OUT / 'novelty.json', json.loads(json.dumps(novelty, default=str)))
    save(OUT / 'collection_summary.json', {'at': stamp(), 'scope': len(scope), 'live_ms': len(cards),
         'missing_ms': len(set(ids) - {c['id'] for c in cards}), 'same_code_cards': len(alternatives),
         'documents': len(docs), 'latest_party': latest, 'blacklist_rows': len(black),
         'novelty_rows': len(novelty), 'external_writes': 0, 'database_writes': 0})
    print(json.dumps(json.loads((OUT / 'collection_summary.json').read_text()), ensure_ascii=False))


if __name__ == '__main__':
    if '--analyze-saved' in sys.argv:
        analyze()
    else:
        collect()
