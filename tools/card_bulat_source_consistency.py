#!/usr/bin/env python3
# поток: card
"""Read-only comparison of Bulat's short feed name with its full title."""
import csv
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/card_bulat_current_description_review_2026-10-12'


def short_resource(text):
    values = re.findall(r'(?<![A-Za-zА-Яа-я\d])(\d+(?:[.,]\d+)?)\s*k\b', text, re.I)
    return round(float(values[-1].replace(',', '.')) * 1000) if values else None


def full_resource(text):
    values = re.findall(r'(?<![\d.])(\d{1,3}(?:[ .]\d{3})+|\d{3,6})\s*стр\b', text, re.I)
    return int(re.sub(r'[ .]', '', values[-1])) if values else None


def volume(text):
    values = re.findall(r'(?<!\d)(\d+(?:[.,]\d+)?)\s*мл\b', text, re.I)
    return float(values[-1].replace(',', '.')) if values else None


def main():
    review = json.loads((OUT / 'review.json').read_text())
    tc = {str(row['external_code']): row for row in json.loads((OUT / 'tc_scope.json').read_text())}
    conflicts = []
    for row in review:
        if row['status'] not in ('ALREADY_EQUAL', 'SAFE_DESCRIPTION_DIFFERENCE'):
            continue
        short, full = row['source_name'], row['proposed_description'] or row['old_description']
        sr, fr, sv, fv = short_resource(short), full_resource(full), volume(short), volume(full)
        kinds = []
        if sr is not None and fr is not None and sr != fr:
            kinds.append('resource')
        if sv is not None and fv is not None and sv != fv:
            kinds.append('volume')
        if not kinds:
            continue
        tcr = tc.get(str(row['external_code']), {}).get('resource')
        conflicts.append({'external_code': row['external_code'], 'article': row['article'],
                          'id': row['id'], 'source_id': row['source_id'], 'stock': row['stock'],
                          'entered': row['entered'], 'kind': ';'.join(kinds),
                          'price_resource': sr, 'full_resource': fr,
                          'tc_resource': tcr, 'price_volume_ml': sv, 'full_volume_ml': fv,
                          'source_name': short, 'full_title': full, 'status': row['status']})
    ready = [row for row in review if row['status'] == 'SAFE_DESCRIPTION_DIFFERENCE'
             and not any(c['id'] == row['id'] for c in conflicts)]
    assert len(conflicts) == 37 and len(ready) == 11
    (OUT / 'source_name_full_title_conflicts.json').write_text(
        json.dumps(conflicts, ensure_ascii=False, indent=2) + '\n')
    with (OUT / 'source_name_full_title_conflicts.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(conflicts[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(conflicts)
    with (OUT / 'validated_description_differences.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['external_code', 'article', 'id', 'stock', 'entered', 'source_name',
                  'old_description', 'proposed_description', 'status']
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(ready)
    summary = {'resource_conflict_rows': sum('resource' in c['kind'] for c in conflicts),
               'volume_conflict_rows': sum('volume' in c['kind'] for c in conflicts),
               'distinct_conflict_cards': len({c['id'] for c in conflicts}),
               'tc_resource_aligns': dict(Counter('price' if c['tc_resource'] == c['price_resource']
                                                  else 'full' if c['tc_resource'] == c['full_resource']
                                                  else 'other' for c in conflicts if 'resource' in c['kind'])),
               'validated_description_differences': len(ready),
               'validated_differences_in_current_receipt': sum(r['entered'] for r in ready),
               'external_writes': 0}
    (OUT / 'consistency_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
