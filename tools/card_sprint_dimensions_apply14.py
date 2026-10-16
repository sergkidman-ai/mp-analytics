#!/usr/bin/env python3
# поток: card
"""One authorized run: fill dimensions on the 14 reviewed Sprint products."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from tools.card_solutionsprint_scraper import login, get, parse_card
from tools.card_sprint_dimensions_prepare import dimensions

SOURCE = ROOT / 'docs/reports/card_sprint_dimensions_prepare_2026-10-14'
OUT = ROOT / 'docs/reports/card_sprint_dimensions_fill14_2026-10-14'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(name, data):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def journal(data):
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps({'at': stamp(), **data}, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def main():
    # Never resume/retry a potentially accepted PUT without a separate audit.
    OUT.mkdir(exist_ok=False)
    plan = json.loads((SOURCE / 'upload_fill_empty_preview.json').read_text())
    review = {r['id']: r for r in json.loads((SOURCE / 'review.json').read_text())}
    assert len(plan) == 14 and len({r['id'] for r in plan}) == 14
    assert all(review[r['id']]['status'] == 'FILL_ALL_THREE' for r in plan)
    assert not any(r['article'] in ('SP353616_MSK', 'SP353617_MSK') for r in plan)
    fields = json.loads((SOURCE / 'summary.json').read_text())['ms_dimension_fields']
    ids = set(fields.values())
    metadata = ms_api.get('/entity/product/metadata/attributes', {'limit': 1000})['rows']
    for name, aid in fields.items():
        assert sum(a['id'] == aid and a['name'] == name and a['type'] == 'string' for a in metadata) == 1
    save('authorization.json', {'at': stamp(), 'user_instruction': 'заполни 14 пустых полей',
                                'scope': '14 reviewed cards, three empty dimensions each',
                                'product_ids': [r['id'] for r in plan], 'excluded_articles': ['SP353616_MSK', 'SP353617_MSK']})
    save('plan.json', plan)
    state = {'status': 'preflight', 'cards': 14, 'fields_per_card': 3, 'write_attempts': 0,
             'writes_http200': 0, 'verified': 0, 'at': stamp()}
    save('summary.json', state)

    def live_guard(r):
        c = ms_api.get('/entity/product/' + r['id'])
        baseline = review[r['id']]
        assert c['id'] == r['id'] and c.get('article') == r['article']
        assert c.get('externalCode') == baseline['external_code'] and not c.get('archived')
        values = [a for a in c.get('attributes', []) if a.get('id') in ids]
        assert len({a['id'] for a in values}) == len(values)
        assert all(a.get('value') is None or str(a['value']).strip() == '' for a in values)
        return c

    try:
        session = requests.Session()
        login(session)
        for r in plan:
            source = parse_card(get(session, r['source_url']).content, r['source_url'], authenticated=True)
            assert source['supplier_code'] == review[r['id']]['supplier_code']
            assert source['properties']['Габариты, мм'] == r['raw_mm']
            assert dimensions(r['raw_mm']) is not None
            save('source/' + r['id'] + '.json', source)
            save('preflight/' + r['id'] + '.json', live_guard(r))
            time.sleep(.5)
        print('Preflight passed: 14 source dimensions unchanged; 42 MS fields empty.', flush=True)
        state['status'] = 'writing'
        save('summary.json', state)
        for r in plan:
            before = live_guard(r)
            save('before/' + r['id'] + '.json', before)
            updates = {a['id']: a for a in r['proposed_payload']['attributes'] if a.get('id') in ids}
            assert set(updates) == ids
            attrs = [{**a, 'value': updates[a['id']]['value']} if a.get('id') in ids else a
                     for a in before.get('attributes', [])]
            present = {a.get('id') for a in before.get('attributes', [])}
            attrs.extend(a for aid, a in updates.items() if aid not in present)
            payload = {'attributes': attrs}
            save('payload/' + r['id'] + '.json', payload)
            journal({'status': 'sending', 'id': r['id'], 'article': r['article']})
            state['write_attempts'] += 1
            save('summary.json', state)
            # Direct requests.put has no automatic retry, unlike core.ms_api.put.
            response = requests.put(ms_api.BASE + '/entity/product/' + r['id'], json=payload,
                                    headers={'Authorization': 'Bearer ' + ms_api.TOKEN}, timeout=(15, 60))
            journal({'status': 'http_response', 'id': r['id'], 'http_status': response.status_code})
            if response.status_code != 200:
                raise RuntimeError('MS write returned HTTP ' + str(response.status_code))
            state['writes_http200'] += 1
            after = ms_api.get('/entity/product/' + r['id'])
            save('after/' + r['id'] + '.json', after)
            new_attrs = {a['id']: a.get('value') for a in after.get('attributes', [])}
            expected_attrs = {a['id']: a.get('value') for a in attrs}
            assert new_attrs == expected_attrs
            for key in ['id', 'name', 'description', 'article', 'externalCode', 'code', 'archived',
                        'supplier', 'weight', 'volume', 'barcodes']:
                assert before.get(key) == after.get(key), key
            journal({'status': 'verified', 'id': r['id'], 'article': r['article'],
                     'dimensions': {name: new_attrs[aid] for name, aid in fields.items()}})
            state['verified'] += 1
            save('summary.json', state)
            print('Verified ' + str(state['verified']) + '/14', flush=True)
            time.sleep(.5)
        state['status'] = 'complete'
        state['completed_at'] = stamp()
        save('summary.json', state)
    except Exception as exc:
        state['status'] = 'stopped_requires_audit'
        state['error_type'] = type(exc).__name__
        save('summary.json', state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        raise SystemExit(1)
    print(json.dumps(state, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
