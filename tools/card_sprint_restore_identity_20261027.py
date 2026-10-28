#!/usr/bin/env python3
# поток: card
"""Restore three Sprint MS product codes from their own archival audit.

The historical article is deliberately held: the same article is now attached
to a different, active TN-245 product in the same supplier group.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api  # noqa: E402

OUT = ROOT / 'docs/reports/card_sprint_restore_identity_2026-10-27'
TARGETS = {
    '62834f21-a83a-11f0-0a80-1453004616dd': ('3149', '3149sp', 'SP107248_MSK'),
    '628ad634-a83a-11f0-0a80-1453004616ec': ('3150', '3150sp', 'SP107249_MSK'),
    '629248b3-a83a-11f0-0a80-1453004616fb': ('3151', '3151sp', 'SP107250_MSK'),
}


def save(name, obj):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def historical_values(product_id):
    rows = list(ms_api.iter_rows('/entity/product/' + product_id + '/audit', page=100))
    changes = [r for r in rows if r.get('eventType') == 'update'
               and {'externalCode', 'code', 'article'} <= set(r.get('diff') or {})]
    assert len(changes) == 1, (product_id, len(changes))
    change = changes[0]
    return {key: change['diff'][key]['oldValue'] for key in ('externalCode', 'code', 'article')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Journal already exists; inspect readback before another run')

    before = {}
    plan = []
    for product_id, expected in TARGETS.items():
        old = historical_values(product_id)
        assert tuple(old[k] for k in ('externalCode', 'code', 'article')) == expected
        card = ms_api.get('/entity/product/' + product_id)
        assert card['id'] == product_id and card.get('archived') is False
        assert card.get('externalCode') not in {t[0] for t in TARGETS.values()}
        assert not card.get('code') and not card.get('article')
        assert card.get('supplier', {}).get('meta', {}).get('href', '').endswith(
            '/9263b7de-97bf-11f0-0a80-015900374bc3')
        codes = ms_api.get('/entity/product', {'filter': 'code=' + old['code'], 'limit': 2})
        assert codes['meta']['size'] == 0, old['code']
        articles = ms_api.get('/entity/product', {'filter': 'article=' + old['article'], 'limit': 10})
        assert articles['meta']['size'] == 1 and articles['rows'][0]['id'] != product_id
        before[product_id] = card
        plan.append({'id': product_id, 'name': card['name'], 'from': {
            'externalCode': card.get('externalCode'), 'code': card.get('code'),
            'article': card.get('article')}, 'to': old,
            'article_held_due_to_conflict_with': articles['rows'][0]['id']})

    assert len(plan) == len(TARGETS) == 3
    save('plan.json', plan)
    save('summary.json', {'at_utc': stamp(), 'scope': 3, 'ready': 3,
                          'fields_to_restore_now': ['externalCode', 'code'],
                          'article_conflicts': 3, 'write_attempts': 0, 'verified': 0,
                          'status': 'ready' if args.apply else 'dry_run'})
    print('Preflight: 3/3 ready; 3/3 article conflicts; 0 writes.' if not args.apply
          else 'Preflight: 3/3 ready; restoring externalCode and code only.', flush=True)
    if not args.apply:
        return

    for product_id, expected in TARGETS.items():
        save('before/' + product_id + '.json', before[product_id])
        live = ms_api.get('/entity/product/' + product_id)
        assert live == before[product_id], product_id
        payload = {'externalCode': expected[0], 'code': expected[1]}
        save('payload/' + product_id + '.json', payload)
        with (OUT / 'journal.jsonl').open('a') as log:
            log.write(json.dumps({'at_utc': stamp(), 'status': 'send_intent', 'id': product_id}) + '\n')
        summary = json.loads((OUT / 'summary.json').read_text())
        summary['write_attempts'] += 1
        summary['status'] = 'writing'
        save('summary.json', summary)
        ms_api.put('/entity/product/' + product_id, payload)
        after = ms_api.get('/entity/product/' + product_id)
        save('after/' + product_id + '.json', after)
        assert after.get('externalCode') == expected[0]
        assert after.get('code') == expected[1]
        assert not after.get('article')
        for key in ('name', 'description', 'supplier', 'attributes', 'barcodes',
                    'weight', 'volume', 'archived'):
            assert after.get(key) == live.get(key), (product_id, key)
        with (OUT / 'journal.jsonl').open('a') as log:
            log.write(json.dumps({'at_utc': stamp(), 'status': 'verified', 'id': product_id}) + '\n')
        summary['verified'] += 1
        save('summary.json', summary)
        print('Verified', summary['verified'], 'of 3', flush=True)
        time.sleep(.5)
    summary['status'] = 'codes_complete_article_held'
    summary['completed_at_utc'] = stamp()
    save('summary.json', summary)


if __name__ == '__main__':
    main()
