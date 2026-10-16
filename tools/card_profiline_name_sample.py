#!/usr/bin/env python3
# поток: card
"""Read-only, stratified live MS sample against saved Profiline catalogue."""
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.db import query
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP
from ops.wb_supplier_chip_evidence import recognize

OUT = ROOT / 'docs/reports/card_profiline_api_access_2026-10-09'


def key(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def classes(value):
    found = set(recognize(value or '', 'Профилайн')['classes'])
    if 'чип без счётчика' in found:
        found.discard('с чипом')
    return ';'.join(sorted(found))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def main():
    data = json.loads((OUT / 'catalog.json').read_text())
    index = defaultdict(list)
    for offer in data['products'].values():
        if (float(offer.get('warehouseStock') or 0) > 0 or float(offer.get('quantity') or 0) > 0) and key(offer.get('vendorCode')):
            index[key(offer['vendorCode'])].append(offer)
    supplier_ids = sorted(sid for sid, group in ID2GROUP.items() if group == 'Профилайн')
    cached = query("""SELECT p.ms_id, p.article, p.external_code,
        p.name, r.payload->>'description' AS description
        FROM ms_product p JOIN raw_moysklad_product r ON r.ms_id=p.ms_id
        WHERE NOT p.archived
        AND regexp_replace(r.payload->'supplier'->'meta'->>'href','^.*/','')=ANY(%s)
        AND upper(regexp_replace(p.article,'\\s','','g'))=ANY(%s)
        ORDER BY p.external_code,p.article,p.ms_id""", (supplier_ids, sorted(index)))
    matched = [c for c in cached if len(index[key(c['article'])]) == 1]
    chosen = []
    def add(c):
        if c['ms_id'] not in {x['ms_id'] for x in chosen} and len(chosen) < 20:
            chosen.append(c)
    # Include explicit chip classes, then both cached equality and differences.
    for cls in ('чип без счётчика', 'с чипом', 'без чипа'):
        for c in [c for c in matched if classes(index[key(c['article'])][0].get('name')) == cls][:4]:
            add(c)
    for equal in (True, False):
        for c in [c for c in matched if (norm(c['description']) == norm(index[key(c['article'])][0].get('name'))) == equal][:5]:
            add(c)
    for c in matched:
        add(c)
        if len(chosen) == 20:
            break
    samples = []
    (OUT / 'ms_sample').mkdir(exist_ok=True)
    for c in chosen:
        live = ms_api.get('/entity/product/' + str(c['ms_id']))
        assert live['id'] == str(c['ms_id']) and not live.get('archived')
        assert ms_api.meta_id(live, 'supplier') in supplier_ids
        assert key(live.get('article')) == key(c['article'])
        offer = index[key(c['article'])][0]
        (OUT / 'ms_sample' / (live['id'] + '.json')).write_text(json.dumps(live, ensure_ascii=False, indent=2))
        name, desc, api_name = live.get('name') or '', live.get('description') or '', offer.get('name') or ''
        samples.append({'ms_id': live['id'], 'external_code': live.get('externalCode'),
                        'article': live.get('article'), 'api_article': offer['vendorCode'],
                        'warehouse_stock': offer.get('warehouseStock'), 'other_stock': offer.get('quantity'),
                        'api_name': api_name, 'ms_name': name, 'ms_description': desc,
                        'name_equal_normalized': norm(name) == norm(api_name),
                        'description_equal_normalized': norm(desc) == norm(api_name),
                        'api_chip': classes(api_name), 'description_chip': classes(desc),
                        'chip_classes_equal': classes(api_name) == classes(desc)})
        time.sleep(.2)
    save('live_name_samples.json', samples)
    if samples:
        with (OUT / 'live_name_samples.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(samples[0]), delimiter=';')
            w.writeheader(); w.writerows(samples)
    summary = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'api_date': data['date'],
               'sample_method': '20 stratified samples: chip classes, cached equal/different descriptions; not representative',
               'stock_scope': 'warehouseStock>0 OR quantity>0', 'article_match': 'unique vendorCode, case/whitespace normalized only',
               'cached_unique_positive_matches': len(matched), 'live_samples': len(samples),
               'names_equal_normalized': sum(r['name_equal_normalized'] for r in samples),
               'descriptions_equal_normalized': sum(r['description_equal_normalized'] for r in samples),
               'chip_class_differences': sum(not r['chip_classes_equal'] for r in samples),
               'sample_api_chip_classes': dict(Counter(r['api_chip'] for r in samples)),
               'external_writes': 0, 'database_writes': 0}
    save('name_sample_summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
