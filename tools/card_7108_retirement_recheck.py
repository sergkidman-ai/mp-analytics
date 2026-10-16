#!/usr/bin/env python3
# поток: card
"""One-off read-only check of the frozen 21 cards after deletion initiated in TC."""
import argparse
import fcntl
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import card_7108_retirement_preflight as api
from collectors.wb_card_content import _content_token
from collectors.ozon import _headers

BASE = ROOT / 'docs/reports/card_7108_retirement_preflight_2026-10-12'
OUT = ROOT / 'docs/reports/card_7108_retirement_recheck_2026-10-12'
DUE = datetime(2026, 10, 12, 9, 18, 36, tzinfo=timezone.utc)


def result(row, state, current_code=None):
    return {**row, 'previous_state': row['state'], 'state': state,
            'current_code': current_code if current_code is not None else row['code']}


def wb(account, rows):
    h = {'Authorization': _content_token(account), 'Content-Type': 'application/json'}
    ids = {int(r['id']) for r in rows}
    active, trash = {}, {}
    for row in rows:
        data = api.read('https://content-api.wildberries.ru/content/v2/get/cards/list', h,
                        {'settings': {'cursor': {'limit': 10},
                                      'filter': {'textSearch': str(row['id']), 'withPhoto': -1}}})
        api.save(f'raw/{account}_active_{row["id"]}.json', data)
        assert isinstance(data.get('cards'), list)
        for card in data['cards']:
            if card['nmID'] in ids:
                active[card['nmID']] = card
        time.sleep(.7)
    cursor, seen = {'limit': 100}, set()
    for page in range(1000):
        data = api.read('https://content-api.wildberries.ru/content/v2/get/cards/trash', h,
                        {'settings': {'cursor': cursor, 'sort': {'ascending': False}}})
        api.save(f'raw/{account}_trash_{page:03d}.json', data)
        assert isinstance(data.get('cards'), list)
        for card in data['cards']:
            if card['nmID'] in ids:
                trash[card['nmID']] = card
        if len(data['cards']) < 100:
            break
        cur = data['cursor']
        key = (cur.get('trashedAt'), cur.get('nmID'))
        if not all(key) or key in seen:
            raise RuntimeError('WB trash traversal incomplete')
        seen.add(key)
        cursor = {'limit': 100, 'trashedAt': key[0], 'nmID': key[1]}
        time.sleep(.7)
    else:
        raise RuntimeError('WB trash pagination limit')
    assert not set(active) & set(trash)
    output = []
    for row in rows:
        card = active.get(int(row['id'])) or trash.get(int(row['id']))
        state = 'active' if int(row['id']) in active else 'trash' if card else 'absent'
        if card and card['vendorCode'] != row['code']:
            state = 'identity_changed'
        output.append(result(row, state, card['vendorCode'] if card else None))
    return output


def ozon(account, rows):
    data = api.read('https://api-seller.ozon.ru/v3/product/info/list', _headers(account),
                    {'product_id': [int(r['id']) for r in rows]})
    api.save(f'raw/{account}_info.json', data)
    assert isinstance(data.get('items'), list)
    cards = {int(c['id']): c for c in data['items']}
    assert set(cards) <= {int(r['id']) for r in rows} and len(cards) == len(data['items'])
    output = []
    for row in rows:
        card = cards.get(int(row['id']))
        state = 'absent' if card is None else 'archive' if (
            card.get('is_archived') or card.get('is_autoarchived')) else 'active'
        if card and card['offer_id'] != row['code']:
            state = 'identity_changed'
        output.append(result(row, state, card['offer_id'] if card else None))
    return output


def market(account, rows):
    index = account.removeprefix('ya_acc')
    h = {'Api-Key': os.environ['YANDEX_API_KEY_ACC' + index], 'Content-Type': 'application/json'}
    businesses = {str(r['business_id']) for r in rows}
    assert len(businesses) == 1
    url = 'https://api.partner.market.yandex.ru/v2/businesses/' + businesses.pop() + '/offer-mappings'
    ids = {str(r['id']) for r in rows}
    data = api.read(url, h, {'offerIds': sorted(ids)}, {'limit': 100})
    api.save(f'raw/{account}_exact.json', data)
    assert not data['result'].get('paging', {}).get('nextPageToken')
    cards = {str(c['offer']['offerId']): c['offer'] for c in data['result']['offerMappings']}
    assert set(cards) <= ids
    # archived and offerIds are mutually exclusive filters. Exhaust the archive
    # to distinguish an archived card from one no longer returned by the API.
    token, seen = None, set()
    for page in range(500):
        params = {'limit': 100}
        if token:
            params['pageToken'] = token
        data = api.read(url, h, {'archived': True}, params)
        api.save(f'raw/{account}_archive_{page:03d}.json', data)
        for c in data['result']['offerMappings']:
            offer = c['offer']
            if str(offer['offerId']) in ids:
                assert offer.get('archived')
                cards[str(offer['offerId'])] = offer
        token = data['result'].get('paging', {}).get('nextPageToken')
        if not token:
            break
        if token in seen:
            raise RuntimeError('Market archive cursor repeated')
        seen.add(token)
        time.sleep(.7)
    else:
        raise RuntimeError('Market archive traversal incomplete')
    return [result(r, 'absent' if str(r['id']) not in cards else
                   'archive' if cards[str(r['id'])].get('archived') else 'active') for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--validate', action='store_true')
    args = parser.parse_args()
    frozen = OUT / 'frozen_cards.json'
    rows = json.loads((frozen if frozen.exists() else BASE / 'all_cards.json').read_text())
    assert len(rows) == len({(r['account'], str(r['id'])) for r in rows}) == 21
    assert all(r['code'].startswith('7108') for r in rows)
    assert sum(r['state'] == 'active' for r in rows) == 17
    if args.validate:
        print('Frozen 21 cards validated; no API calls')
        return
    OUT.mkdir(exist_ok=True)
    api.OUT = OUT  # Fresh output directory; the original preflight stays intact.
    with (OUT / 'check.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if datetime.now(timezone.utc) < DUE:
            raise RuntimeError('Not due yet')
        if (OUT / 'summary.json').exists():
            raise RuntimeError('Prior check exists; no automatic rerun')
        api.save('frozen_cards.json', rows)
        started = datetime.now(timezone.utc).isoformat()
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = []
            for account in sorted({r['account'] for r in rows}):
                group = [r for r in rows if r['account'] == account]
                fn = wb if account.startswith('wb_') else ozon if account.startswith('oz_') else market
                jobs.append(pool.submit(fn, account, group))
            checked = [r for job in jobs for r in job.result()]
        assert len(checked) == 21
        api.save('all_cards.json', checked)
        summary = {'status': 'complete', 'started_at': started,
                   'finished_at': datetime.now(timezone.utc).isoformat(), 'total': 21,
                   'states': dict(Counter(r['state'] for r in checked)),
                   'still_active': [r for r in checked if r['state'] == 'active'],
                   'identity_changed': [r for r in checked if r['state'] == 'identity_changed'],
                   'marketplace_writes': 0, 'database_writes': 0}
        api.save('summary.json', summary)
        api.save('task_status.json', {'status': 'complete', 'finished_at': summary['finished_at']})
        report = ['# Повторная проверка 7108 после удаления из ТК', '',
                  'Проверено 21; состояния: ' + json.dumps(summary['states'], ensure_ascii=False), '',
                  '| Кабинет | Код | Было | Стало |', '|---|---|---|---|']
        report.extend(f"| {r['account']} | {r['code']} | {r['previous_state']} | {r['state']} |" for r in checked)
        (OUT / 'README.md').write_text('\n'.join(report) + '\n')
        print(json.dumps({'total': 21, 'states': summary['states']}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        OUT.mkdir(exist_ok=True)
        api.OUT = OUT
        api.save('task_status.json', {'status': 'failed', 'error_type': type(exc).__name__,
                                      'at': datetime.now(timezone.utc).isoformat()})
        raise
