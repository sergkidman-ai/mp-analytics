#!/usr/bin/env python3
# поток: card
"""Fresh read-only Sprint dimensions and weight for every active Sprint MS card."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from tools.card_solutionsprint_current_review import IDS
from tools import card_sprint_dimensions_prepare as prior

OUT = ROOT / 'docs/reports/card_sprint_dimensions_weight_2026-10-23'
NAMES = ('Длина, см.', 'Ширина, см.', 'Высота, см.')


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def load(name):
    return json.loads((OUT / name).read_text())


def weight(raw):
    found = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(кг|г|гр)\.?\s*', str(raw or ''), re.I)
    if not found:
        return None
    try:
        value = Decimal(found[1].replace(',', '.'))
    except InvalidOperation:
        return None
    return value / 1000 if found[2].lower() in ('г', 'гр') and value > 0 else value if value > 0 else None


def decimal(value):
    try:
        parsed = Decimal(str(value or '0').strip().replace(',', '.'))
        return parsed if parsed.is_finite() and parsed > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def get_ms():
    def supplier(sid):
        return list(ms_api.iter_rows('/entity/product', {'filter':
                    f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=100))
    with ThreadPoolExecutor(max_workers=2) as pool:
        groups = list(pool.map(supplier, IDS))
    cards = [card for group in groups for card in group]
    if len(cards) != len({card['id'] for card in cards}):
        raise RuntimeError('Duplicate MS product ID')
    save('ms_active_snapshot.json', cards)
    save('ms_snapshot_meta.json', {'at_utc': datetime.now(timezone.utc).isoformat(),
         'cards': len(cards), 'supplier_groups': {IDS[sid]: len(group) for sid, group in zip(IDS, groups)},
         'unique_numeric_codes': len({prior.code(c) for c in cards if prior.code(c)})})
    print(f'MS active cards: {len(cards)}; unique Sprint codes: '
          f'{len({prior.code(c) for c in cards if prior.code(c)})}', flush=True)
    return cards


def collect():
    if (OUT / 'ms_active_snapshot.json').exists():
        cards = load('ms_active_snapshot.json')
    else:
        cards = get_ms()
    if not cards:
        raise RuntimeError('Empty MS scope')
    prior.OUT = OUT
    source = prior.collect(cards)
    if len(source) != len({prior.code(c) for c in cards if prior.code(c)}):
        raise RuntimeError('Supplier collection is incomplete')
    return cards, source


def fmt(value):
    return format(value.normalize(), 'f') if value is not None else ''


def analyze():
    cards = load('ms_active_snapshot.json')
    source = {r['supplier_code']: r for r in load('supplier_cards.json')}
    review = []
    for card in cards:
        code = prior.code(card)
        found = source.get(code, {})
        properties = found.get('properties') or {}
        dimensions = prior.dimensions(properties.get('Габариты, мм', ''))
        gross = weight(properties.get('Вес', ''))
        attrs = {a.get('name'): a.get('value') for a in card.get('attributes', [])}
        old_dimensions = [decimal(attrs.get(n)) for n in NAMES]
        old_weight = decimal(card.get('weight'))
        dimensions_suspect = bool(dimensions and (
            min(dimensions) < Decimal('0.5') or max(dimensions) > Decimal('100')
            or dimensions[0] * dimensions[1] * dimensions[2] > Decimal('200000')
            or ('лазер' in properties.get('Вид товара', '').lower()
                and min(dimensions) < Decimal('1'))))
        weight_suspect = bool(gross and (gross < Decimal('0.005') or gross > Decimal('25')))
        if not code:
            dims_status = weight_status = 'ARTICLE_UNMATCHED'
        elif found.get('status') != 'READ_OK':
            dims_status = weight_status = found.get('status', 'SOURCE_MISSING')
        else:
            if dimensions is None:
                dims_status = 'SOURCE_MISSING_OR_INVALID'
            elif dimensions_suspect:
                dims_status = 'SOURCE_SUSPECT_REVIEW'
            elif old_dimensions == dimensions:
                dims_status = 'ALREADY_EQUAL'
            elif all(value is None for value in old_dimensions):
                dims_status = 'FILL_EMPTY'
            elif any(value is None for value in old_dimensions):
                dims_status = 'PARTIAL_REVIEW'
            else:
                dims_status = 'DIFFERENT_REVIEW'
            if gross is None:
                weight_status = 'SOURCE_MISSING_OR_INVALID'
            elif weight_suspect:
                weight_status = 'SOURCE_SUSPECT_REVIEW'
            elif old_weight == gross:
                weight_status = 'ALREADY_EQUAL'
            elif old_weight is None:
                weight_status = 'FILL_EMPTY'
            else:
                weight_status = 'DIFFERENT_REVIEW'
        review.append({'id': card['id'], 'supplier_group': IDS.get(ms_api.meta_id(card, 'supplier'), ''),
                       'external_code': card.get('externalCode', ''), 'article': card.get('article', ''),
                       'supplier_code': code or '', 'supplier_name': found.get('name', ''),
                       'supplier_url': found.get('url', ''), 'supplier_dimensions_mm': properties.get('Габариты, мм', ''),
                       'supplier_weight_raw': properties.get('Вес', ''),
                       'length_cm': fmt(dimensions[0]) if dimensions else '',
                       'width_cm': fmt(dimensions[1]) if dimensions else '',
                       'height_cm': fmt(dimensions[2]) if dimensions else '',
                       'weight_kg': fmt(gross),
                       'ms_length_cm': fmt(old_dimensions[0]), 'ms_width_cm': fmt(old_dimensions[1]),
                       'ms_height_cm': fmt(old_dimensions[2]), 'ms_weight_kg': fmt(old_weight),
                       'dimensions_status': dims_status, 'weight_status': weight_status,
                       'supplier_stock_positive': found.get('stock_positive'),
                       'source_at_utc': found.get('collected_at_utc', '')})
    save('review.json', review)
    with (OUT / 'review.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(review[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(review)
    summary = {'at_utc': datetime.now(timezone.utc).isoformat(), 'ms_cards': len(cards),
               'source_unique_codes': len(source), 'source_statuses': dict(Counter(r['status'] for r in source.values())),
               'dimensions': dict(Counter(r['dimensions_status'] for r in review)),
               'weight': dict(Counter(r['weight_status'] for r in review)),
               'supplier_stock_positive_cards': sum(r['supplier_stock_positive'] is True for r in review),
               'supplier_stock_unknown_cards': sum(r['supplier_stock_positive'] is None for r in review),
               'size_source_unit': 'mm; source order retained; MS centimetres',
               'size_axes_explicitly_named_by_supplier': False,
               'weight_unit': 'parsed only from explicit kg or g suffix',
               'supplier_weight_label': 'Вес; supplier does not explicitly label gross/net',
               'write_authorized': False, 'ms_writes': 0}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['collect', 'analyze', 'all'])
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.action in ('collect', 'all'):
        collect()
    if args.action in ('analyze', 'all'):
        analyze()


if __name__ == '__main__':
    main()
