#!/usr/bin/env python3
# поток: card
"""Read saved Rapid catalogue, compare stock-positive exact articles to MS. No writes to MS/DB."""
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.db import query
from collectors.moysklad import _get, API
from invoice_bot.supplier_groups import ID2GROUP
from ops.wb_supplier_chip_evidence import recognize

OUT = ROOT / 'docs/reports/card_chip_wave2_recheck_2026-10-07'
RAW = OUT / 'raw/rapid_api_2026-10-08'


def key(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def chip_classes(text):
    result = recognize(text or '', 'Суперфайн')
    values = set(result['classes'])
    if not re.search(r'чип|\bchip\b', text or '', re.I):
        values.discard('чип без счётчика')
    if 'чип без счётчика' in values:
        values.discard('с чипом')
    return ';'.join(sorted(values))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2) + '\n')


def csv_save(name, rows):
    if not rows:
        return
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


def main():
    data = json.loads((RAW / 'catalogue.json').read_text())
    assert data['error']['code'] == 0
    items = data['price']
    positive = [r for r in items if float(r['UnitsFree']) > 0]
    index = defaultdict(list)
    for row in positive:
        index[key(row['CodeID'])].append(row)
    wave_codes = json.loads((OUT / 'scope.json').read_text())['codes']
    ids = [sid for sid, group in ID2GROUP.items() if group == 'Суперфайн']
    assert ids
    cards = query("""SELECT s.captured_at,s.ms_id,s.external_code,s.stock,p.article,
        p.name,r.payload->>'description' AS description,p.archived
        FROM supplier_stock s JOIN ms_product p ON p.ms_id=s.ms_id
        JOIN raw_moysklad_product r ON r.ms_id=s.ms_id
        WHERE s.captured_at=(SELECT max(captured_at) FROM supplier_stock)
        AND s.store='Удаленный склад' AND s.stock>0 AND s.external_code=ANY(%s)
        AND split_part(r.payload->'supplier'->'meta'->>'href','/',9)=ANY(%s)
        ORDER BY s.external_code,p.article""", (wave_codes, ids))
    # Href may have differing slash counts: fail closed rather than match other suppliers.
    if not cards:
        cards = query("""SELECT s.captured_at,s.ms_id,s.external_code,s.stock,p.article,
            p.name,r.payload->>'description' AS description,p.archived
            FROM supplier_stock s JOIN ms_product p ON p.ms_id=s.ms_id
            JOIN raw_moysklad_product r ON r.ms_id=s.ms_id
            WHERE s.captured_at=(SELECT max(captured_at) FROM supplier_stock)
            AND s.store='Удаленный склад' AND s.stock>0 AND s.external_code=ANY(%s)
            AND regexp_replace(r.payload->'supplier'->'meta'->>'href','^.*/','')=ANY(%s)
            ORDER BY s.external_code,p.article""", (wave_codes, ids))
    linked, unmatched = [], []
    for card in cards:
        matches = index.get(key(card['article']), [])
        if len(matches) != 1:
            unmatched.append({**card, 'status': 'NO_UNIQUE_POSITIVE_API_ARTICLE', 'matches': len(matches)})
            continue
        row = matches[0]
        linked.append({'external_code': card['external_code'], 'ms_id': card['ms_id'],
                       'article': card['article'], 'api_article': row['CodeID'],
                       'ms_stock_date': str(card['captured_at']), 'ms_remote_stock': str(card['stock']),
                       'api_free_stock': row['UnitsFree'], 'api_name': row['Name'],
                       'api_chip_classes': chip_classes(row['Name']),
                       'cached_ms_name': card['name'], 'cached_ms_description': card['description'] or '',
                       'cached_description_equals_api': (card['description'] or '').strip() == row['Name'].strip(),
                       'cached_description_normalized_equals_api': normalized(card['description']) == normalized(row['Name'])})
    save('10_rapid_linked_cards.json', linked)
    save('10_rapid_unmatched_cards.json', unmatched)
    csv_save('10_rapid_stock_positive_evidence.csv', linked)
    chosen = []
    for typ in ('чип без счётчика', 'с чипом', 'без чипа', ''):
        matches = [r for r in linked if r['api_chip_classes'] == typ]
        if matches:
            chosen.append(matches[0])
    for row in sorted(linked, key=lambda r: (r['cached_description_normalized_equals_api'], r['external_code'], r['article'])):
        if len(chosen) >= 8:
            break
        if row['ms_id'] not in {r['ms_id'] for r in chosen}:
            chosen.append(row)
    samples = []
    for row in chosen:
        live = _get(f"{API}/entity/product/{row['ms_id']}")
        assert live['id'] == row['ms_id'] and live['externalCode'] == row['external_code']
        assert live.get('article') == row['article']
        assert live.get('supplier', {}).get('meta', {}).get('href', '').rstrip('/').split('/')[-1] in ids
        (RAW / f"ms_{row['ms_id']}.json").write_text(json.dumps(live, ensure_ascii=False, indent=2))
        description = live.get('description') or ''
        samples.append({**row, 'live_ms_name': live['name'], 'live_ms_description': description,
                        'description_equals_api': description.strip() == row['api_name'].strip(),
                        'description_normalized_equals_api': normalized(description) == normalized(row['api_name']),
                        'name_equals_api': live['name'].strip() == row['api_name'].strip(),
                        'description_chip_classes': chip_classes(description),
                        'chip_classes_equal': chip_classes(description) == row['api_chip_classes']})
    csv_save('10_rapid_live_ms_samples.csv', samples)
    fields = sorted({k for row in items for k in row})
    summary = {'at': datetime.now(timezone.utc).isoformat(), 'api_catalogue_date': data['date'],
               'catalogue_items': len(items), 'free_stock_positive_items': len(positive),
               'fields': fields, 'dedicated_chip_fields': [f for f in fields if re.search(r'chip|чип|count|счет', f, re.I)],
               'positive_api_name_classes': dict(Counter(chip_classes(r['Name']) for r in positive)),
               'wave_positive_ms_cards': len(cards), 'wave_linked_positive_api_cards': len(linked),
               'wave_linked_codes': len({r['external_code'] for r in linked}),
               'wave_positive_api_classes': dict(Counter(r['api_chip_classes'] for r in linked)),
               'wave_unmatched_cards': len(unmatched),
               'cached_description_equals_api': sum(r['cached_description_equals_api'] for r in linked),
               'cached_description_normalized_equals_api': sum(r['cached_description_normalized_equals_api'] for r in linked),
               'live_ms_samples': len(samples), 'live_description_equals_api': sum(r['description_equals_api'] for r in samples),
               'live_description_normalized_equals_api': sum(r['description_normalized_equals_api'] for r in samples),
               'live_name_equals_api': sum(r['name_equals_api'] for r in samples),
               'live_description_chip_mismatches': sum(not r['chip_classes_equal'] for r in samples),
               'external_writes': 0, 'database_writes': 0}
    save('10_rapid_summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'fields'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
