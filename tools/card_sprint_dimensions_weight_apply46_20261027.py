#!/usr/bin/env python3
# поток: card
"""One-shot authorized fill of 44 empty Sprint dimensions and 3 empty weights."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from tools.card_solutionsprint_scraper import get, login, parse_card
from tools.card_sprint_dimensions_prepare import dimensions
from tools.card_sprint_dimensions_weight_collect_20261023 import weight

SOURCE = ROOT / 'docs/reports/card_sprint_dimensions_weight_2026-10-23'
OUT = ROOT / 'docs/reports/card_sprint_dimensions_weight_fill46_2026-10-27'
NAMES = ('Длина, см.', 'Ширина, см.', 'Высота, см.')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def journal(**event):
    with (OUT / 'journal.jsonl').open('a') as handle:
        handle.write(json.dumps({'at_utc': stamp(), **event}, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def read_csv(name):
    with (SOURCE / name).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle, delimiter=';'))


def dec(value):
    return Decimal(str(value).replace(',', '.'))


def empty(value):
    return value is None or str(value).strip() == ''


def main():
    OUT.mkdir(exist_ok=False)
    size_rows = read_csv('dimensions_fill_44.csv')
    weight_rows = read_csv('weight_fill_3.csv')
    assert len(size_rows) == 44 and len(weight_rows) == 3
    assert all(r['dimensions_status'] == 'FILL_EMPTY' for r in size_rows)
    assert all(r['weight_status'] == 'FILL_EMPTY' for r in weight_rows)
    by_id = {}
    for row in size_rows + weight_rows:
        old = by_id.setdefault(row['id'], row)
        assert old['article'] == row['article'] and old['supplier_code'] == row['supplier_code']
    assert len(by_id) == 46
    targets = []
    size_ids = {r['id'] for r in size_rows}
    weight_ids = {r['id'] for r in weight_rows}
    for pid, row in sorted(by_id.items(), key=lambda item: (item[1]['supplier_code'], item[1]['article'])):
        targets.append({'id': pid, 'article': row['article'], 'external_code': row['external_code'],
                        'supplier_code': row['supplier_code'], 'supplier_url': row['supplier_url'],
                        'dimensions': [row['length_cm'], row['width_cm'], row['height_cm']] if pid in size_ids else None,
                        'weight': row['weight_kg'] if pid in weight_ids else None})
    save('authorization.json', {'instruction': 'запусти сейчас запись 46 карточек по размеру и весу',
         'scope': '44 cards with three empty dimensions, 3 cards with empty weight, 1 overlap',
         'source_report': str(SOURCE.relative_to(ROOT)), 'created_at_utc': stamp()})
    save('plan.json', targets)
    summary = {'status': 'preflight', 'cards': 46, 'size_cards': 44, 'weight_cards': 3,
               'verified': 0, 'write_attempts': 0, 'http200': 0, 'started_at_utc': stamp()}
    save('summary.json', summary)

    metadata = ms_api.get('/entity/product/metadata/attributes', {'limit': 1000})['rows']
    fields = {}
    for name in NAMES:
        matches = [a for a in metadata if a['name'] == name and a['type'] == 'string']
        assert len(matches) == 1, name
        fields[name] = matches[0]
    save('attribute_fields.json', {n: {'id': a['id'], 'meta': a['meta']} for n, a in fields.items()})

    # Read every current supplier page before any external write. A changed value
    # is a hard stop; the 23 October review is never silently broadened.
    urls = {}
    for target in targets:
        code = target['supplier_code']
        if code in urls:
            assert urls[code] == target['supplier_url']
        urls[code] = target['supplier_url']
    session = requests.Session()
    login(session)
    current_source = {}
    for number, (code, url) in enumerate(sorted(urls.items()), 1):
        source = parse_card(get(session, url).content, url, authenticated=True)
        assert source['supplier_code'] == code
        current_source[code] = source
        save(f'source/{code}.json', source)
        if number % 10 == 0:
            print(f'Source checked {number}/{len(urls)}', flush=True)
        time.sleep(.25)
    for target in targets:
        props = current_source[target['supplier_code']]['properties']
        if target['dimensions'] is not None:
            current = dimensions(props.get('Габариты, мм', ''))
            assert current is not None and current == [dec(v) for v in target['dimensions']], target['article']
            assert min(current) >= Decimal('0.5') and max(current) <= Decimal('100')
        if target['weight'] is not None:
            assert weight(props.get('Вес', '')) == dec(target['weight']), target['article']

    # Save all fresh MS snapshots and check identity / emptiness before sending
    # the first PUT. On a later drift, stop instead of replacing an entered value.
    before_all = {}
    for target in targets:
        card = ms_api.get('/entity/product/' + target['id'])
        assert card['id'] == target['id'] and not card.get('archived')
        assert card.get('article') == target['article'] and card.get('externalCode') == target['external_code']
        attrs = {a['id']: a.get('value') for a in card.get('attributes', [])}
        if target['dimensions'] is not None:
            assert all(empty(attrs.get(fields[name]['id'])) for name in NAMES), target['article']
        if target['weight'] is not None:
            assert empty(card.get('weight')) or dec(card['weight']) == 0, target['article']
        before_all[target['id']] = card
        save(f'before/{target["id"]}.json', card)
    print('Preflight passed: 38 current supplier pages, 46 live MS cards.', flush=True)
    summary['status'] = 'writing'
    save('summary.json', summary)

    for target in targets:
        pid = target['id']
        prior = before_all[pid]
        live = ms_api.get('/entity/product/' + pid)
        assert live == prior, target['article']
        body = {}
        if target['dimensions'] is not None:
            updates = {fields[name]['id']: value for name, value in zip(NAMES, target['dimensions'])}
            attrs = [{**a, 'value': updates[a['id']]} if a.get('id') in updates else a
                     for a in live.get('attributes', [])]
            existing = {a.get('id') for a in attrs}
            attrs.extend({'meta': fields[name]['meta'], 'id': fields[name]['id'], 'value': value}
                         for name, value in zip(NAMES, target['dimensions'])
                         if fields[name]['id'] not in existing)
            body['attributes'] = attrs
        if target['weight'] is not None:
            body['weight'] = float(dec(target['weight']))
        save(f'payload/{pid}.json', body)
        journal(status='sending', id=pid, article=target['article'])
        summary['write_attempts'] += 1
        save('summary.json', summary)
        # One attempt only. A transport failure might follow a successful write;
        # the journal then requires a read-only audit before any retry.
        response = requests.put(ms_api.BASE + '/entity/product/' + pid, json=body,
                                headers={'Authorization': 'Bearer ' + ms_api.TOKEN}, timeout=(15, 60))
        journal(status='http_response', id=pid, http_status=response.status_code)
        if response.status_code != 200:
            raise RuntimeError('MS PUT returned HTTP ' + str(response.status_code))
        summary['http200'] += 1
        after = ms_api.get('/entity/product/' + pid)
        save(f'after/{pid}.json', after)
        if target['dimensions'] is not None:
            got = {a['id']: a.get('value') for a in after.get('attributes', [])}
            wanted = {a['id']: a.get('value') for a in body['attributes']}
            assert got == wanted, target['article']
        else:
            assert after.get('attributes', []) == live.get('attributes', []), target['article']
        if target['weight'] is not None:
            assert dec(after['weight']) == dec(target['weight']), target['article']
        else:
            assert after.get('weight') == live.get('weight'), target['article']
        for key in ('name', 'article', 'externalCode', 'code', 'description', 'supplier',
                    'barcodes', 'volume', 'archived'):
            assert after.get(key) == live.get(key), (target['article'], key)
        journal(status='verified', id=pid, article=target['article'])
        summary['verified'] += 1
        save('summary.json', summary)
        if summary['verified'] % 10 == 0 or summary['verified'] == len(targets):
            print(f"Verified {summary['verified']}/{len(targets)}", flush=True)
        time.sleep(.3)
    summary['status'] = 'complete'
    summary['completed_at_utc'] = stamp()
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if OUT.exists() and (OUT / 'summary.json').exists():
            current = json.loads((OUT / 'summary.json').read_text())
            current['status'] = 'stopped_requires_audit'
            current['error_type'] = type(exc).__name__
            save('summary.json', current)
        print('Stopped:', type(exc).__name__, str(exc)[:160], flush=True)
        raise SystemExit(1)
