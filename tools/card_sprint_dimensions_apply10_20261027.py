#!/usr/bin/env python3
# поток: card
"""One-shot fill of the ten Sprint dimensions explicitly confirmed by owner."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
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

SOURCE = ROOT / 'docs/reports/card_sprint_dimensions_weight_2026-10-23/dimensions_review_10.csv'
OUT = ROOT / 'docs/reports/card_sprint_dimensions_fill10_2026-10-27'
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


def main():
    OUT.mkdir(exist_ok=False)
    with SOURCE.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=';'))
    assert len(rows) == 10 and len({r['id'] for r in rows}) == 10
    assert all(r['dimensions_status'] == 'SOURCE_SUSPECT_REVIEW' for r in rows)
    assert all(not any(r[x] for x in ('ms_length_cm', 'ms_width_cm', 'ms_height_cm')) for r in rows)
    save('authorization.json', {'instruction': 'тут все ок с размерами можно тоже записать сейчас',
         'scope': 'ten Sprint cards from dimensions_review_10.csv; dimensions only',
         'created_at_utc': stamp()})
    save('plan.json', rows)
    summary = {'status': 'preflight', 'cards': 10, 'write_attempts': 0, 'http200': 0,
               'verified': 0, 'started_at_utc': stamp()}
    save('summary.json', summary)

    metadata = ms_api.get('/entity/product/metadata/attributes', {'limit': 1000})['rows']
    fields = {}
    for name in NAMES:
        found = [a for a in metadata if a['name'] == name and a['type'] == 'string']
        assert len(found) == 1, name
        fields[name] = found[0]

    url_by_code = {}
    for row in rows:
        code = row['supplier_code']
        if code in url_by_code:
            assert url_by_code[code] == row['supplier_url']
        url_by_code[code] = row['supplier_url']
    assert len(url_by_code) == 5
    session = requests.Session()
    login(session)
    fresh_source = {}
    for code, url in sorted(url_by_code.items()):
        card = parse_card(get(session, url).content, url, authenticated=True)
        assert card['supplier_code'] == code
        fresh_source[code] = card
        save(f'source/{code}.json', card)
        time.sleep(.25)
    for row in rows:
        measured = dimensions(fresh_source[row['supplier_code']]['properties'].get('Габариты, мм', ''))
        wanted = [Decimal(row[name]) for name in ('length_cm', 'width_cm', 'height_cm')]
        assert measured == wanted, row['article']

    before = {}
    for row in rows:
        card = ms_api.get('/entity/product/' + row['id'])
        assert card['id'] == row['id'] and not card.get('archived')
        assert card.get('article') == row['article'] and card.get('externalCode') == row['external_code']
        values = {a['id']: a.get('value') for a in card.get('attributes', [])}
        assert all(values.get(fields[name]['id']) in (None, '') for name in NAMES), row['article']
        before[row['id']] = card
        save(f'before/{row["id"]}.json', card)
    print('Preflight passed: 5 current supplier pages and 10 empty MS cards.', flush=True)
    summary['status'] = 'writing'
    save('summary.json', summary)

    for row in rows:
        pid = row['id']
        live = ms_api.get('/entity/product/' + pid)
        assert live == before[pid], row['article']
        updates = {fields[name]['id']: row[key] for name, key in zip(NAMES, ('length_cm', 'width_cm', 'height_cm'))}
        attrs = [{**a, 'value': updates[a['id']]} if a.get('id') in updates else a
                 for a in live.get('attributes', [])]
        present = {a.get('id') for a in attrs}
        attrs.extend({'meta': fields[name]['meta'], 'id': fields[name]['id'], 'value': row[key]}
                     for name, key in zip(NAMES, ('length_cm', 'width_cm', 'height_cm'))
                     if fields[name]['id'] not in present)
        payload = {'attributes': attrs}
        save(f'payload/{pid}.json', payload)
        journal(status='sending', id=pid, article=row['article'])
        summary['write_attempts'] += 1
        save('summary.json', summary)
        response = requests.put(ms_api.BASE + '/entity/product/' + pid, json=payload,
                                headers={'Authorization': 'Bearer ' + ms_api.TOKEN}, timeout=(15, 60))
        journal(status='http_response', id=pid, http_status=response.status_code)
        if response.status_code != 200:
            raise RuntimeError('MS PUT returned HTTP ' + str(response.status_code))
        summary['http200'] += 1
        after = ms_api.get('/entity/product/' + pid)
        save(f'after/{pid}.json', after)
        actual = {a['id']: a.get('value') for a in after.get('attributes', [])}
        expected = {a['id']: a.get('value') for a in attrs}
        assert actual == expected, row['article']
        for key in ('name', 'article', 'externalCode', 'code', 'description', 'supplier',
                    'barcodes', 'weight', 'volume', 'archived'):
            assert after.get(key) == live.get(key), (row['article'], key)
        journal(status='verified', id=pid, article=row['article'])
        summary['verified'] += 1
        save('summary.json', summary)
        print(f"Verified {summary['verified']}/10", flush=True)
        time.sleep(.3)
    summary['status'] = 'complete'
    summary['completed_at_utc'] = stamp()
    save('summary.json', summary)


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
