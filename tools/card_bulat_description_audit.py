#!/usr/bin/env python3
# поток: card
"""Read-only: stock-positive Bulat feed names versus live MS descriptions."""
import argparse
import csv
import hashlib
import html
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP
from ops.wb_supplier_chip_evidence import recognize

OUT = ROOT / 'docs/reports/card_bulat_description_audit_2026-10-09'


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def article_key(text):
    return re.sub(r'\s+', '', str(text or '')).upper()


def normalized(text):
    return re.sub(r'\s+', ' ', html.unescape(str(text or '')).replace('\xa0', ' ')).strip()


def classes(text):
    values = set(recognize(text or '', 'Булат')['classes'])
    if not re.search(r'чип|\bchip\b', text or '', re.I):
        values.discard('чип без счётчика')
    if 'чип без счётчика' in values:
        values.discard('с чипом')
    return ';'.join(sorted(values))


def write_csv(name, rows):
    if not rows:
        return
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--saved', action='store_true', help='Reanalyse saved live snapshot and feed without new requests')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.saved:
        content = (OUT / 'clientapi.xml').read_bytes()
        previous = json.loads((OUT / 'summary.json').read_text())
        last_modified = previous.get('feed_last_modified')
    else:
        response = requests.get('https://bulat-group.ru/price/clientapi.xml', timeout=(15, 90))
        response.raise_for_status()
        content = response.content
        last_modified = response.headers.get('Last-Modified')
        (OUT / 'clientapi.xml').write_bytes(content)
    root = ET.fromstring(content)
    assert root.tag == 'yml_catalog'
    offers = root.findall('.//offer')
    positive = []
    for offer in offers:
        count = float((offer.findtext('count') or '0').replace(',', '.'))
        if count > 0:
            positive.append({'offer_id': offer.get('id') or '',
                             'vendorCode': (offer.findtext('vendorCode') or '').strip(),
                             'name': offer.findtext('name') or '',
                             'description': offer.findtext('description') or '', 'count': count})
    index = defaultdict(dict)
    for item in positive:
        for value in (item['offer_id'], item['vendorCode']):
            if article_key(value):
                index[article_key(value)][item['offer_id']] = item
    ids = {sid for sid, group in ID2GROUP.items() if group == 'Булат'}
    cards = {}
    if args.saved:
        cards = {r['id']: r for r in json.loads((OUT / 'ms_live_snapshot.json').read_text())}
    else:
        for sid in sorted(ids):
            for row in ms_api.iter_rows('/entity/product',
                                       {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=500):
                assert not row.get('archived') and ms_api.meta_id(row, 'supplier') in ids
                cards[row['id']] = row
        save('ms_live_snapshot.json', list(cards.values()))
    report = []
    for card in cards.values():
        matches = list(index.get(article_key(card.get('article')), {}).values())
        row = {'ms_id': card['id'], 'external_code': card.get('externalCode') or '',
               'ms_code': card.get('code') or '', 'article': card.get('article') or '',
               'supplier_id': ms_api.meta_id(card, 'supplier'), 'status': '',
               'feed_offer_id': '', 'feed_vendorCode': '', 'feed_count': '', 'match_field': '',
               'feed_name': '', 'ms_name': card.get('name') or '',
               'ms_description': card.get('description') or '', 'feed_chip_classes': '',
               'ms_description_chip_classes': '', 'chip_classes_differ': '',
               'ms_name_equals_feed_normalized': '', 'feed_description_starts_with_ms_description': ''}
        if not matches:
            row['status'] = 'NO_POSITIVE_FEED_MATCH'
        elif len(matches) != 1:
            row['status'] = 'AMBIGUOUS_FEED_MATCH'
        else:
            item = matches[0]
            row.update({'feed_offer_id': item['offer_id'], 'feed_vendorCode': item['vendorCode'],
                        'feed_count': item['count'], 'feed_name': item['name'],
                        'match_field': 'offer_id' if article_key(card.get('article')) == article_key(item['offer_id']) else 'vendorCode'})
            old, new = row['ms_description'], item['name']
            plain = normalized(re.sub(r'<[^>]+>', ' ', html.unescape(item['description'])))
            row['feed_description_starts_with_ms_description'] = bool(normalized(old) and plain.startswith(normalized(old)))
            if not new.strip():
                row['status'] = 'EMPTY_FEED_NAME'
            else:
                row['feed_chip_classes'] = classes(new)
                row['ms_description_chip_classes'] = classes(old)
                row['chip_classes_differ'] = classes(new) != classes(old)
                row['ms_name_equals_feed_normalized'] = normalized(row['ms_name']) == normalized(new)
                row['status'] = ('EXACT' if old == new else 'WHITESPACE_OR_ENTITY_ONLY' if normalized(old) == normalized(new)
                                 else 'CASE_ONLY' if normalized(old).casefold() == normalized(new).casefold()
                                 else 'CONTENT_DIFFERENCE')
        report.append(row)
    report.sort(key=lambda r: (r['external_code'], r['article'], r['ms_id']))
    write_csv('all_ms_cards.csv', report)
    compared = [r for r in report if r['status'] in ('EXACT', 'WHITESPACE_OR_ENTITY_ONLY', 'CASE_ONLY', 'CONTENT_DIFFERENCE')]
    changed = [r for r in compared if r['status'] != 'EXACT']
    substantive = [r for r in compared if r['status'] == 'CONTENT_DIFFERENCE']
    chip = [r for r in compared if r['chip_classes_differ']]
    write_csv('description_differences.csv', changed)
    write_csv('content_differences.csv', substantive)
    write_csv('chip_information_differences.csv', chip)
    save('examples.json', (chip[:5] + [r for r in substantive if r not in chip][:5]))
    summary = {'at': previous['at'] if args.saved else datetime.now(timezone.utc).isoformat(),
               'feed_date': root.attrib.get('date'), 'feed_sha256': hashlib.sha256(content).hexdigest(),
               'feed_last_modified': last_modified,
               'feed_total_offers': len(offers), 'feed_positive_offers': len(positive),
               'positive_offers_without_name': sum(not r['name'].strip() for r in positive),
               'active_ms_cards': len(cards), 'card_statuses': dict(Counter(r['status'] for r in report)),
               'compared_cards': len(compared), 'compared_external_codes': len({r['external_code'] for r in compared}),
               'exact_copy_candidates_if_using_feed_name': len(changed), 'description_content_difference_cards': len(substantive),
               'different_name_but_feed_description_starts_with_ms_description': sum(r['feed_description_starts_with_ms_description'] for r in substantive),
               'chip_information_difference_cards': len(chip),
               'chip_information_difference_external_codes': len({r['external_code'] for r in chip}),
               'chip_transition_counts': dict(Counter(f"{r['ms_description_chip_classes'] or 'не указан'} -> {r['feed_chip_classes'] or 'не указан'}" for r in chip)),
               'matched_ms_names_equal_feed_normalized': sum(r['ms_name_equals_feed_normalized'] for r in compared),
               'matching_fields': dict(Counter(r['match_field'] for r in compared)),
               'external_writes': 0, 'database_writes': 0}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
