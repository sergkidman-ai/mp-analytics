#!/usr/bin/env python3
# поток: card
"""Audit-gated continuation after HTTP 429: only three still-empty Sprint cards."""
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from tools.card_solutionsprint_scraper import get, login, parse_card
from tools.card_sprint_dimensions_prepare import dimensions

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
    plan = json.loads((OUT / 'plan.json').read_text())
    audit = {r['id']: r['status'] for r in json.loads((OUT / 'post_429_audit.json').read_text())}
    assert len(plan) == len(audit) == 10
    pending = [r for r in plan if audit[r['id']] == 'empty']
    assert [r['article'] for r in pending] == ['SP87408_MSK', 'SP26351', 'SP87408']
    assert sum(v == 'desired' for v in audit.values()) == 7
    metadata = ms_api.get('/entity/product/metadata/attributes', {'limit': 1000})['rows']
    fields = {}
    for name in NAMES:
        found = [a for a in metadata if a['name'] == name and a['type'] == 'string']
        assert len(found) == 1
        fields[name] = found[0]

    session = requests.Session()
    login(session)
    for code in sorted({r['supplier_code'] for r in pending}):
        row = next(r for r in pending if r['supplier_code'] == code)
        card = parse_card(get(session, row['supplier_url']).content, row['supplier_url'], authenticated=True)
        assert card['supplier_code'] == code
        save(f'resume_source/{code}.json', card)
        for r in (x for x in pending if x['supplier_code'] == code):
            assert dimensions(card['properties'].get('Габариты, мм', '')) == [
                Decimal(r[k]) for k in ('length_cm', 'width_cm', 'height_cm')]

    current = {}
    for row in plan:
        card = ms_api.get('/entity/product/' + row['id'])
        assert card['id'] == row['id'] and card['article'] == row['article'] and not card.get('archived')
        values = {a['id']: a.get('value') for a in card.get('attributes', [])}
        actual = [values.get(fields[name]['id']) for name in NAMES]
        expected = [row[k] for k in ('length_cm', 'width_cm', 'height_cm')]
        if audit[row['id']] == 'desired':
            assert all(v not in (None, '') and Decimal(str(v)) == Decimal(w)
                       for v, w in zip(actual, expected))
        else:
            assert all(v in (None, '') for v in actual)
            assert card == json.loads((OUT / 'before' / (row['id'] + '.json')).read_text())
            current[row['id']] = card
            save(f'resume_before/{row["id"]}.json', card)
    print('Resume preflight passed: 7 desired, 3 still empty; source refreshed.', flush=True)
    summary = json.loads((OUT / 'summary.json').read_text())
    assert summary['verified'] == 7 and summary['status'] == 'stopped_requires_audit'
    summary['status'] = 'resuming_after_429_audit'
    save('summary.json', summary)

    for row in pending:
        time.sleep(20)  # Space writes after the earlier HTTP 429.
        pid = row['id']
        before = current[pid]
        live = ms_api.get('/entity/product/' + pid)
        assert live == before, row['article']
        updates = {fields[name]['id']: row[key] for name, key in zip(NAMES, ('length_cm', 'width_cm', 'height_cm'))}
        attrs = [{**a, 'value': updates[a['id']]} if a.get('id') in updates else a
                 for a in live.get('attributes', [])]
        present = {a.get('id') for a in attrs}
        attrs.extend({'meta': fields[name]['meta'], 'id': fields[name]['id'], 'value': row[key]}
                     for name, key in zip(NAMES, ('length_cm', 'width_cm', 'height_cm'))
                     if fields[name]['id'] not in present)
        payload = {'attributes': attrs}
        save(f'resume_payload/{pid}.json', payload)
        journal(status='resume_sending', id=pid, article=row['article'])
        summary['write_attempts'] += 1
        save('summary.json', summary)
        response = requests.put(ms_api.BASE + '/entity/product/' + pid, json=payload,
                                headers={'Authorization': 'Bearer ' + ms_api.TOKEN}, timeout=(15, 60))
        journal(status='resume_http_response', id=pid, http_status=response.status_code)
        if response.status_code != 200:
            raise RuntimeError('MS PUT returned HTTP ' + str(response.status_code))
        summary['http200'] += 1
        after = ms_api.get('/entity/product/' + pid)
        save(f'resume_after/{pid}.json', after)
        assert {a['id']: a.get('value') for a in after.get('attributes', [])} == {
            a['id']: a.get('value') for a in attrs}
        for key in ('name', 'article', 'externalCode', 'code', 'description', 'supplier',
                    'barcodes', 'weight', 'volume', 'archived'):
            assert after.get(key) == live.get(key), (row['article'], key)
        journal(status='resume_verified', id=pid, article=row['article'])
        summary['verified'] += 1
        save('summary.json', summary)
        print(f"Verified {summary['verified']}/10", flush=True)
    summary['status'] = 'complete'
    summary['completed_at_utc'] = stamp()
    save('summary.json', summary)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if (OUT / 'summary.json').exists():
            summary = json.loads((OUT / 'summary.json').read_text())
            summary['status'] = 'stopped_requires_audit'
            summary['error_type'] = type(exc).__name__
            save('summary.json', summary)
        print('Stopped:', type(exc).__name__, str(exc)[:160], flush=True)
        raise SystemExit(1)
