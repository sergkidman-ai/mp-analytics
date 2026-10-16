#!/usr/bin/env python3
# поток: card
"""Read-only current positive-stock Bulat audit; never change the prior frozen write report."""
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from core.db import query
from prices import loader, blacklist
from prices.profiles import get_identity
from tools.card_bulat_full_title_sync import IDS, extract_title, APPROVED_TITLES
from tools.card_bulat_description_audit import article_key, normalized, classes

OUT = ROOT / 'docs/reports/card_bulat_current_description_review_2026-10-12'
PREVIOUS = ROOT / 'docs/reports/card_bulat_full_title_sync_2026-10-09'


def current_title(description):
    title, why = extract_title(description)
    if not why:
        return title, why
    # Feed-specific redundant manufacturer annotation, after a complete title.
    # Remove only a small list of brands; never discard chip/firmware information.
    first = re.split(r'\s{2,}', description.strip(), maxsplit=1)[0]
    found = re.match(r'^(.*?)\s+и\s+Для устройств:\s*(.*?)\s*$', first, re.I)
    if found and len(found[2]) < 100 and re.fullmatch(
        r'(?:[A-Za-zА-Яа-яЁё+\-]+[;,\s]*)+', found[2]
    ) and not re.search(r'чип|сч[её]т|chip|firmware', found[2], re.I):
        return extract_title(found[1])
    return title, why


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, default=str, indent=2) + '\n')


def main():
    root = ET.fromstring((OUT / 'clientapi.xml').read_bytes())
    old_root = ET.fromstring((PREVIOUS / 'clientapi.xml').read_bytes())
    old_offers = {o.get('id'): o for o in old_root.findall('.//offer')}
    offers = root.findall('.//offer')
    assert len({o.get('id') for o in offers}) == len(offers)
    index = defaultdict(dict)
    for o in offers:
        for value in [o.get('id'), *(v.text for v in o.findall('vendorCode'))]:
            if value and article_key(value):
                index[article_key(value)][o.get('id')] = o
    # This is a supplier-specific active collection, not the global MS catalogue.
    # Only exact current-feed matches with count>0 enter the audit; historical
    # cards with no live feed match are never analysed or proposed for changes.
    def fetch(sid):
        return list(ms_api.iter_rows('/entity/product',
            {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=100))
    if '--saved' in sys.argv:
        cards = json.loads((OUT / 'ms_active_snapshot.json').read_text())
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            cards = [c for rs in pool.map(fetch, sorted(IDS)) for c in rs]
    assert len({c['id'] for c in cards}) == len(cards)
    assert all(not c.get('archived') and ms_api.meta_id(c, 'supplier') in IDS for c in cards)
    save('ms_active_snapshot.json', cards)
    docs = json.loads((OUT / 'current_docs.json').read_text()) if '--saved' in sys.argv else loader.existing_docs(get_identity('bulat'))
    latest = max(d['name'].rsplit('_p', 1)[0] for d in docs)
    docs = [d for d in docs if d['name'].startswith(latest + '_p')]

    def fetchdoc(doc):
        d = ms_api.get('/entity/enter/' + doc['id'] + '/positions', {'limit': 1000})
        assert d['meta']['size'] <= 1000
        return {'id': doc['id'], 'name': doc['name'], 'positions': d['rows']}
    if '--saved' in sys.argv:
        positions = json.loads((OUT / 'current_positions.json').read_text())
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            positions = list(pool.map(fetchdoc, docs))
    save('current_positions.json', positions)
    entered = defaultdict(list)
    for d in positions:
        for pos in d['positions']:
            entered[ms_api.meta_id(pos, 'assortment')].append({'document': d['name'], 'quantity': pos['quantity']})
    black = {r['article_norm']: r for r in query('SELECT * FROM prc_blacklist WHERE article_norm=ANY(%s)',
                                                ([str(c.get('article') or '').strip().upper() for c in cards],))}
    save('relevant_blacklist.json', list(black.values()))
    review = []
    not_current_positive = 0
    for c in cards:
        matches = list(index.get(article_key(c.get('article')), {}).values())
        positive = [o for o in matches if float((o.findtext('count') or '0').replace(',', '.')) > 0]
        if not positive:
            not_current_positive += 1
            continue
        row = {'id': c['id'], 'external_code': c.get('externalCode') or '', 'article': c.get('article') or '',
               'supplier_id': ms_api.meta_id(c, 'supplier'), 'ms_name': c.get('name') or '',
               'old_description': c.get('description') or '', 'source_id': '', 'source_name': '',
               'source_full_description': '', 'proposed_description': '', 'stock': '', 'status': '',
               'entered': bool(entered[c['id']]), 'positions': entered[c['id']], 'title_method': ''}
        # Resolve the complete feed, not only its stock-positive subset: stock
        # must never silently choose between two different article identities.
        if len(matches) != 1:
            row['status'] = 'AMBIGUOUS_CURRENT_FEED_ARTICLE'
            review.append(row)
            continue
        o = matches[0]
        row.update(source_id=o.get('id'), source_name=o.findtext('name') or '',
                   source_full_description=o.findtext('description') or '',
                   stock=float((o.findtext('count') or '0').replace(',', '.')))
        title, why = current_title(row['source_full_description'])
        approved = APPROVED_TITLES.get((row['external_code'], row['source_id']))
        old_o = old_offers.get(row['source_id'])
        same_approved_source = approved and old_o is not None and (
            normalized(old_o.findtext('description')) == normalized(row['source_full_description'])
            and normalized(old_o.findtext('name')) == normalized(row['source_name']))
        if same_approved_source:
            title, why = approved, ''
            row['title_method'] = 'PREVIOUS_USER_APPROVAL_UNCHANGED_SOURCE'
        else:
            row['title_method'] = 'SAFE_TITLE_BEFORE_PROSE' if title else 'UNCLEAR'
        row['proposed_description'] = title
        relevant_name = title or row['source_name']
        # Prefix-only kinds: mention of paper inside a cartridge's compatibility
        # text must never exclude that cartridge.
        kind_excluded = re.search(r'^\s*(?:фото)?бумаг|^\s*пл[её]нк|^\s*термотрансфер', relevant_name, re.I)
        original = re.search(r'\bоригинал(?:ьный|ьная|ьное|ьные)?\b|\boriginal\b', relevant_name, re.I)
        damaged = blacklist.in_damaged(row['source_name'])
        if 'брак' in row['ms_name'].lower():
            row['status'] = 'EXCLUDED_DEFECTIVE_MS_CARD'
        elif row['article'].strip().upper() in black:
            row['status'] = 'EXCLUDED_BLACKLIST'
        elif kind_excluded:
            row['status'] = 'EXCLUDED_PAPER_FILM_TRANSFER'
        elif original:
            row['status'] = 'EXCLUDED_ORIGINAL'
        elif damaged or blacklist.in_kinds(row['source_name']):
            row['status'] = 'EXCLUDED_SUPPLIER_RULE'
        elif why:
            row['status'] = ('PUBLISHED_NAME_EQUAL_FULL_TITLE_UNVERIFIED'
                if normalized(row['old_description']).casefold() == normalized(row['source_name']).casefold()
                else why)
        elif len(title) < len(normalized(row['old_description'])) * .75:
            row['status'] = ('PUBLISHED_NAME_EQUAL_FULL_TITLE_UNVERIFIED'
                if normalized(row['old_description']).casefold() == normalized(row['source_name']).casefold()
                else 'POSSIBLY_TRUNCATED_TITLE')
        elif normalized(row['old_description']).casefold() == normalized(title).casefold():
            row['status'] = 'ALREADY_EQUAL'
        elif classes(row['old_description']) and classes(title) != classes(row['old_description']):
            row['status'] = ('PUBLISHED_NAME_EQUAL_FULL_TITLE_UNVERIFIED'
                if normalized(row['old_description']).casefold() == normalized(row['source_name']).casefold()
                else 'CHIP_INFORMATION_CHANGE_REQUIRES_REVIEW')
        else:
            row['status'] = 'SAFE_DESCRIPTION_DIFFERENCE'
        review.append(row)
    # Prefer the card actually used in the current receipt over its duplicate
    # alias outside receipts. Historical aliases are preserved as exclusions,
    # never proposed for updates or manual investigation.
    grouped = defaultdict(list)
    ambiguous = []
    for r in review:
        if r['source_id']:
            grouped[r['source_id']].append(r)
        else:
            ambiguous.append(r)
    selected, shadowed = [], []
    for group in grouped.values():
        current = [r for r in group if r['entered']]
        if current:
            selected.extend(current)
            shadowed.extend(r for r in group if not r['entered'])
        else:
            # No stock receipt: keep actual ID-based supplier articles first.
            direct = [r for r in group if article_key(r['article']) == article_key(r['source_id'])]
            selected.extend(direct or group)
            shadowed.extend(r for r in group if direct and r not in direct)
    review = selected + ambiguous
    save('excluded_duplicate_aliases.json', shadowed)
    save('review.json', review)
    columns = list(review[0]) if review else []
    for name, rs in [('all_current_positive_cards.csv', review),
                     ('description_differences.csv', [r for r in review if r['status'] == 'SAFE_DESCRIPTION_DIFFERENCE']),
                     ('needs_source_review.csv', [r for r in review if r['status'] in
                         ('NO_SAFE_TITLE_BOUNDARY', 'UNSUPPORTED_HTML', 'EMPTY_SOURCE_DESCRIPTION',
                          'POSSIBLY_TRUNCATED_TITLE', 'CHIP_INFORMATION_CHANGE_REQUIRES_REVIEW',
                          'AMBIGUOUS_CURRENT_FEED_ARTICLE')]),
                     ('positive_not_in_enter.csv', [r for r in review if not r['entered']])]:
        with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=columns, delimiter=';', extrasaction='ignore')
            w.writeheader()
            for r in rs:
                w.writerow({**r, 'positions': json.dumps(r['positions'], ensure_ascii=False)})
    matched_offers = {r['source_id'] for r in review if r['source_id']}
    positive_offers = {o.get('id') for o in offers if float((o.findtext('count') or '0').replace(',', '.'))>0}
    # Source offers absent from our catalogue are not automatically new saleable
    # goods: the feed includes spare parts, chips and other non-target categories.
    missing = []
    for o in offers:
        if o.get('id') in positive_offers - matched_offers:
            missing.append({'id': o.get('id'), 'name': o.findtext('name'), 'count': o.findtext('count'),
                            'vendor_codes': [v.text for v in o.findall('vendorCode') if v.text]})
    save('positive_feed_without_current_ms_match.json', missing)
    articles = [r['article'].strip().upper() for r in review]
    novel = query("SELECT * FROM prc_novelty WHERE supplier_key='bulat' AND article_norm=ANY(%s)", (articles,))
    save('relevant_novelty.json', novel)
    summary = {'status': 'complete_read_only', 'at': datetime.now(timezone.utc).isoformat(),
               'feed': json.loads((OUT / 'feed_metadata.json').read_text()), 'active_supplier_cards': len(cards),
               'current_positive_ms_cards': len(review), 'represented_source_offers': len(matched_offers),
               'outside_current_positive_scope': not_current_positive, 'current_enter_documents': len(docs),
               'positive_cards_in_enter': sum(r['entered'] for r in review),
               'historical_duplicate_aliases_excluded': len(shadowed),
               'statuses': dict(Counter(r['status'] for r in review)),
               'positive_source_offers_without_current_ms_match': len(missing),
               'write_fields_if_later_authorized': ['description'], 'external_writes': 0, 'database_writes': 0}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
