#!/usr/bin/env python3
# поток: card
"""Explain frozen 495 Profiline exclusions; suggestions are review only."""
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from difflib import get_close_matches
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.card_profiline_name_sample import key

OUT = ROOT / 'docs/reports/card_profiline_description_prepare_2026-10-09'


def loose(value):
    return re.sub(r'[^A-ZА-Я0-9]', '', key(value))


def main():
    data = json.loads((OUT / 'catalog.json').read_text())
    cards = {x['id']: x for x in json.loads((OUT / 'ms_before.json').read_text())}
    excluded = [x for x in json.loads((OUT / 'review.json').read_text())
                if x['status'] in ('NO_SOURCE_ARTICLE', 'AMBIGUOUS_SOURCE_ARTICLE')]
    offers = list(data['products'].values())
    by_article, by_loose, by_barcode = defaultdict(list), defaultdict(list), defaultdict(list)
    for o in offers:
        if key(o.get('vendorCode')):
            by_article[key(o['vendorCode'])].append(o)
            by_loose[loose(o['vendorCode'])].append(o)
        if str(o.get('barCode') or '').strip():
            by_barcode[str(o['barCode']).strip()].append(o)
    result = []
    for row in excluded:
        c = cards[row['id']]
        barcodes = {str(v).strip() for b in c.get('barcodes', []) for v in b.values() if v}
        candidates, evidence = [], ''
        if row['status'] == 'AMBIGUOUS_SOURCE_ARTICLE':
            candidates = by_article[key(row['article'])]
            evidence = 'DUPLICATE_EXACT_API_ARTICLE'
        elif not key(row['article']):
            evidence = 'EMPTY_MS_ARTICLE'
        else:
            hits = {o.get('code') or json.dumps(o, sort_keys=True): o
                    for b in barcodes for o in by_barcode.get(b, [])}
            if hits:
                candidates = list(hits.values())
                evidence = 'BARCODE_MATCH_REQUIRES_IDENTITY_REVIEW'
            elif by_loose.get(loose(row['article'])):
                candidates = by_loose[loose(row['article'])]
                evidence = 'PUNCTUATION_ONLY_ARTICLE_CANDIDATE'
            else:
                nearest = get_close_matches(loose(row['article']), sorted(by_loose), n=3, cutoff=.85)
                candidates = [o for k in nearest for o in by_loose[k]][:5]
                evidence = 'SIMILAR_ARTICLE_REVIEW_ONLY' if candidates else 'NO_CANDIDATE_AT_0_85_THRESHOLD'
        details = [{'article':o.get('vendorCode'), 'name':o.get('name'),
                    'barcode':o.get('barCode'), 'source_code':o.get('code'),
                    'warehouse_stock':o.get('warehouseStock'), 'other_stock':o.get('quantity')}
                   for o in candidates]
        result.append({**row, 'ms_barcodes': ', '.join(sorted(barcodes)),
                       'api_exact_matches':len(by_article.get(key(row['article']), [])),
                       'review_evidence':evidence, 'review_candidates':details,
                       'automatic_write_allowed':False})
    assert len(result)==495 and len({x['id'] for x in result})==495
    (OUT / 'unmatched_495_detailed.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    csv_rows = [{**r, 'review_candidates':json.dumps(r['review_candidates'],ensure_ascii=False)} for r in result]
    with (OUT / 'unmatched_495_detailed.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(csv_rows[0]),delimiter=';');w.writeheader();w.writerows(csv_rows)
    summary={'total':len(result),'original_reasons':dict(Counter(r['status'] for r in result)),
             'review_evidence_counts':dict(Counter(r['review_evidence'] for r in result)),
             'proposed_automatic_updates':0,'stock_filter':False,
             'note':'Barcode and similar-article matches are candidate evidence only; not approved identity mapping.'}
    (OUT / 'unmatched_495_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    main()
