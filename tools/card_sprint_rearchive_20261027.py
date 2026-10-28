#!/usr/bin/env python3
# поток: card
"""Return three obsolete Sprint TN-241 cards to the MS archive, clearing IDs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api  # noqa: E402

OUT = ROOT / 'docs/reports/card_sprint_rearchive_2026-10-27'
TARGETS = {
    '62834f21-a83a-11f0-0a80-1453004616dd': ('3149', '3149sp'),
    '628ad634-a83a-11f0-0a80-1453004616ec': ('3150', '3150sp'),
    '629248b3-a83a-11f0-0a80-1453004616fb': ('3151', '3151sp'),
}


def save(name, obj):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def log(event, product_id, **extra):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'journal.jsonl').open('a') as handle:
        handle.write(json.dumps({'at_utc': stamp(), 'event': event, 'id': product_id,
                                 **extra}, ensure_ascii=False) + '\n')
        handle.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Existing journal; audit before any repeat')

    before = {}
    plan = []
    for product_id, (external_code, code) in TARGETS.items():
        card = ms_api.get('/entity/product/' + product_id)
        assert card['id'] == product_id and card.get('archived') is False
        assert (card.get('externalCode'), card.get('code'), card.get('article')) == (
            external_code, code, None)
        assert card.get('supplier', {}).get('meta', {}).get('href', '').endswith(
            '/9263b7de-97bf-11f0-0a80-015900374bc3')
        report = ms_api.get('/report/stock/bystore', {
            'filter': f'product={ms_api.BASE}/entity/product/{product_id}', 'limit': 100})
        assert report['meta']['size'] == 0, (product_id, 'nonzero stock report')
        assert not report.get('rows')
        before[product_id] = card
        plan.append({'id': product_id, 'name': card.get('name'), 'externalCode': external_code,
                     'code': code, 'article': None, 'stock_report_rows': 0,
                     'action': 'clear article/code/externalCode and archive'})
    assert len(plan) == 3
    save('plan.json', plan)
    save('summary.json', {'at_utc': stamp(), 'scope': 3, 'ready': 3,
                          'stock_report_rows': 0, 'write_attempts': 0, 'verified': 0,
                          'status': 'ready' if args.apply else 'dry_run'})
    print('Preflight: 3 exact active cards; 0 stock rows; 3 ready.', flush=True)
    if not args.apply:
        return

    for product_id, (external_code, _) in TARGETS.items():
        save('before/' + product_id + '.json', before[product_id])
        live = ms_api.get('/entity/product/' + product_id)
        assert live == before[product_id], product_id
        payload = {'archived': True, 'article': '', 'code': '', 'externalCode': ''}
        save('payload/' + product_id + '.json', payload)
        log('send_intent', product_id)
        summary = json.loads((OUT / 'summary.json').read_text())
        summary['status'] = 'writing'
        summary['write_attempts'] += 1
        save('summary.json', summary)
        response = requests.put(ms_api.BASE + '/entity/product/' + product_id,
                                json=payload,
                                headers={'Authorization': 'Bearer ' + ms_api.TOKEN},
                                timeout=(15, 60))
        log('http_response', product_id, http_status=response.status_code)
        if response.status_code != 200:
            raise RuntimeError('MS PUT returned HTTP ' + str(response.status_code))
        after = ms_api.get('/entity/product/' + product_id)
        save('after/' + product_id + '.json', after)
        assert after.get('archived') is True
        assert not after.get('article') and not after.get('code')
        assert after.get('externalCode') and after['externalCode'] != external_code
        assert not re.fullmatch(r'\d{4}', after['externalCode'])
        for key in ('name', 'description', 'supplier', 'attributes', 'barcodes',
                    'weight', 'volume'):
            assert after.get(key) == live.get(key), (product_id, key)
        log('verified', product_id, new_external_code=after['externalCode'])
        summary['verified'] += 1
        save('summary.json', summary)
        print('Verified', summary['verified'], 'of 3', flush=True)
    summary['status'] = 'complete'
    summary['completed_at_utc'] = stamp()
    save('summary.json', summary)


if __name__ == '__main__':
    main()
