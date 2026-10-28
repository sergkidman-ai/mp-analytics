#!/usr/bin/env python3
# поток: card
"""One authorized Sprint weight replacement using fresh supplier pages and MS readback."""
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
from tools.card_sprint_dimensions_weight_collect_20261023 import weight

SOURCE = ROOT / 'docs/reports/card_sprint_dimensions_weight_2026-10-23/weight_review_74.csv'
OUT = ROOT / 'docs/reports/card_sprint_weight_replace74_2026-10-27'
BATCH = 20


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


def read_rows():
    with SOURCE.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=';'))
    assert len(rows) == 74 and len({r['id'] for r in rows}) == 74
    assert all(r['weight_status'] == 'DIFFERENT_REVIEW' for r in rows)
    return rows


def same_identity(live, row):
    return (live['id'] == row['id'] and not live.get('archived') and
            live.get('article') == row['article'] and
            live.get('externalCode') == row['external_code'])


def main():
    OUT.mkdir(exist_ok=False)
    rows = read_rows()
    save('authorization.json', {'instruction': 'у поставщика первоисточник данных по весу и размеру; значения можно перезаписать',
         'scope': '74 current Sprint weight disagreements only; source Вес with explicit kg/g',
         'source_report': str(SOURCE.relative_to(ROOT)), 'created_at_utc': stamp()})
    state = {'status': 'preflight', 'reviewed': 74, 'source_pages': 0, 'already_equal': 0,
             'planned': 0, 'write_batches': 0, 'write_attempts': 0, 'http200': 0,
             'verified': 0, 'started_at_utc': stamp()}
    save('summary.json', state)
    urls = {}
    for row in rows:
        code = row['supplier_code']
        if code in urls:
            assert urls[code] == row['supplier_url']
        urls[code] = row['supplier_url']
    assert len(urls) == 57
    session = requests.Session()
    login(session)
    source_by_code = {}
    for index, (code, url) in enumerate(sorted(urls.items()), 1):
        card = parse_card(get(session, url).content, url, authenticated=True)
        assert card['supplier_code'] == code
        source_by_code[code] = card
        save(f'source/{code}.json', card)
        state['source_pages'] = index
        if index % 15 == 0:
            save('summary.json', state)
            print(f'Fresh supplier pages {index}/{len(urls)}', flush=True)
        time.sleep(.3)

    plan = []
    before = {}
    for index, row in enumerate(rows, 1):
        raw = source_by_code[row['supplier_code']]['properties'].get('Вес', '')
        target = weight(raw)
        assert target is not None and Decimal('0.005') <= target <= Decimal('25'), row['article']
        live = ms_api.get('/entity/product/' + row['id'])
        assert same_identity(live, row), row['article']
        current = Decimal(str(live.get('weight') or 0))
        baseline = Decimal(row['ms_weight_kg'])
        if current == target:
            state['already_equal'] += 1
            continue
        assert current == baseline, row['article']
        item = {'id': row['id'], 'article': row['article'],
                'external_code': row['external_code'], 'supplier_code': row['supplier_code'],
                'source_weight_raw': raw, 'before_weight_kg': str(current),
                'target_weight_kg': str(target)}
        plan.append(item)
        before[row['id']] = live
        save(f'before/{row["id"]}.json', live)
        if index % 20 == 0:
            print(f'Current MS weights checked {index}/{len(rows)}', flush=True)
        time.sleep(.25)
    state['planned'] = len(plan)
    save('plan.json', plan)
    save('summary.json', state)
    assert len(plan) + state['already_equal'] == 74
    print(f"Preflight passed: {len(plan)} writes, {state['already_equal']} already equal.", flush=True)
    state['status'] = 'writing'
    save('summary.json', state)

    for start in range(0, len(plan), BATCH):
        chunk = plan[start:start+BATCH]
        payload = []
        for item in chunk:
            live = ms_api.get('/entity/product/' + item['id'])
            assert live == before[item['id']], item['article']
            payload.append({'id': item['id'], 'weight': float(Decimal(item['target_weight_kg']))})
            time.sleep(.25)
        name = f'batch_{start:03d}'
        save(f'payload/{name}.json', payload)
        journal(status='sending', batch=name, ids=[p['id'] for p in payload])
        state['write_attempts'] += len(payload)
        save('summary.json', state)
        try:
            response = requests.post(ms_api.BASE + '/entity/product', json=payload,
                                     headers={'Authorization': 'Bearer ' + ms_api.TOKEN},
                                     timeout=(15, 120))
        except requests.RequestException:
            raise RuntimeError('Uncertain MS batch write; audit before any retry') from None
        journal(status='http_response', batch=name, http_status=response.status_code)
        if response.status_code != 200:
            raise RuntimeError('MS batch rejected HTTP ' + str(response.status_code))
        returned = response.json()
        if not isinstance(returned, list) or {r.get('id') for r in returned} != {p['id'] for p in payload}:
            raise RuntimeError('MS batch response IDs mismatch; audit before retry')
        state['write_batches'] += 1
        state['http200'] += len(payload)
        save('summary.json', state)
        for item in chunk:
            old = before[item['id']]
            after = ms_api.get('/entity/product/' + item['id'])
            save(f'after/{item["id"]}.json', after)
            assert Decimal(str(after.get('weight'))) == Decimal(item['target_weight_kg']), item['article']
            for field in ('name', 'article', 'externalCode', 'code', 'description',
                          'supplier', 'barcodes', 'volume', 'archived', 'attributes'):
                assert after.get(field) == old.get(field), (item['article'], field)
            journal(status='verified', id=item['id'], article=item['article'])
            state['verified'] += 1
            if state['verified'] % 10 == 0 or state['verified'] == len(plan):
                save('summary.json', state)
                print(f"Verified {state['verified']}/{len(plan)}", flush=True)
            time.sleep(.3)
        save('summary.json', state)
        if start + BATCH < len(plan):
            time.sleep(15)
    state['status'] = 'complete'
    state['completed_at_utc'] = stamp()
    save('summary.json', state)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if OUT.exists() and (OUT / 'summary.json').exists():
            state = json.loads((OUT / 'summary.json').read_text())
            state['status'] = 'stopped_requires_audit'
            state['error_type'] = type(exc).__name__
            save('summary.json', state)
        print('Stopped:', type(exc).__name__, str(exc)[:160], flush=True)
        raise SystemExit(1)
