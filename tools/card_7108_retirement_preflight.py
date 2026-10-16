#!/usr/bin/env python3
# поток: card
"""Read-only discovery of 7108 derivatives and comparison with 5292."""
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from core import ms_api
from core.db import query
from collectors.wb_card_content import _content_token
from collectors.ozon import _headers

OUT = ROOT / 'docs/reports/card_7108_retirement_preflight_2026-10-12'


def save(name, data):
    p = OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=2) + '\n')


def read(url, headers, body=None, params=None):
    # Every POST endpoint here is a documented read; no mutation endpoint exists.
    allowed = ['/content/v2/get/cards/list', '/content/v2/get/cards/trash',
               '/v3/product/list', '/v3/product/info/list', '/v2/campaigns']
    if not any(url.endswith(p) for p in allowed) and not (
        url.startswith('https://api.partner.market.yandex.ru/v2/businesses/')
        and url.endswith('/offer-mappings')
    ):
        raise ValueError('Outside read-only allowlist')
    for attempt in range(5):
        r = requests.get(url, headers=headers, params=params, timeout=60) if body is None else requests.post(
            url, headers=headers, json=body, params=params, timeout=60)
        if r.status_code in (420, 429) or r.status_code >= 500:
            time.sleep(min(30, 3 * (attempt + 1)))
            continue
        r.raise_for_status()
        data = r.json()
        if data.get('error') or data.get('errors') or data.get('status') == 'ERROR':
            raise RuntimeError('API read rejected')
        return data
    raise RuntimeError('Read retries exhausted')


def wb_account(account):
    result = []
    for account in (account,):
        h = {'Authorization': _content_token(account), 'Content-Type': 'application/json'}
        for state, endpoint in [('active', 'list'), ('trash', 'trash')]:
            cursor = {'limit': 100}
            seen = set()
            for page in range(1000):
                settings = {'cursor': cursor, 'sort': {'ascending': False}}
                # Trash textSearch is unreliable: exhaust the actual trash.
                if state == 'active':
                    # textSearch matches the exact article, not its derivatives.
                    # Exhaust the catalogue to find previously unknown suffixes.
                    settings['filter'] = {'withPhoto': -1}
                path = OUT / f'raw/{account}_{state}_{page:03d}.json'
                # Resume only a contiguous, already-read traversal. The original
                # active page zero used exact textSearch, so it must be replaced.
                if path.exists() and (page > 0 or state == 'trash'):
                    data = json.loads(path.read_text())
                else:
                    data = read('https://content-api.wildberries.ru/content/v2/get/cards/' + endpoint,
                                h, {'settings': settings})
                    save(f'raw/{account}_{state}_{page:03d}.json', data)
                assert isinstance(data.get('cards'), list)
                for c in data['cards']:
                    if str(c.get('vendorCode', '')).startswith('7108'):
                        result.append(dict(platform='WB', account=account, code=c['vendorCode'],
                                           id=c['nmID'], state=state, title=c.get('title', '')))
                if len(data['cards']) < 100:
                    break
                cur = data['cursor']
                key = (cur.get('updatedAt') or cur.get('trashedAt'), cur['nmID'])
                if key in seen:
                    raise RuntimeError('WB cursor repeated')
                seen.add(key)
                cursor = {'limit': 100, 'nmID': cur['nmID']}
                cursor['trashedAt' if state == 'trash' else 'updatedAt'] = key[0]
                if not (OUT / f'raw/{account}_{state}_{page+1:03d}.json').exists():
                    time.sleep(.4)
            else:
                raise RuntimeError('WB pagination incomplete')
        print(account, 'verified', sum(r['account'] == account for r in result), flush=True)
    return result


def wb():
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = [r for batch in pool.map(wb_account, ('wb_acc1', 'wb_acc2')) for r in batch]
    save('wb_cards.json', result)
    return result


def ozon():
    result = []
    for account in ('oz_acc1', 'oz_acc2'):
        h = _headers(account)
        targets = {}
        for visibility in ('ALL', 'ARCHIVED'):
            last = ''
            seen = set()
            for page in range(200):
                data = read('https://api-seller.ozon.ru/v3/product/list', h,
                            {'filter': {'visibility': visibility}, 'last_id': last, 'limit': 1000})
                save(f'raw/{account}_{visibility}_{page:03d}.json', data)
                batch = data['result']['items']
                for c in batch:
                    if str(c.get('offer_id', '')).startswith('7108'):
                        targets[c['product_id']] = (c['offer_id'], visibility)
                following = data['result'].get('last_id', '')
                if len(batch) < 1000 or not following:
                    break
                if following in seen:
                    raise RuntimeError('Ozon cursor repeated')
                seen.add(following)
                last = following
                time.sleep(.4)
            else:
                raise RuntimeError('Ozon pagination incomplete')
        data = read('https://api-seller.ozon.ru/v3/product/info/list', h,
                    {'product_id': list(targets)}) if targets else {'items': []}
        save(f'raw/{account}_target_info.json', data)
        assert {c['id'] for c in data['items']} == set(targets)
        for c in data['items']:
            result.append(dict(platform='Ozon', account=account, code=c['offer_id'],
                               id=c['id'], sku=c.get('sku'), title=c.get('name', ''),
                               state='archive' if c.get('is_archived') or c.get('is_autoarchived') else 'active'))
        print(account, 'verified', sum(r['account'] == account for r in result), flush=True)
    save('ozon_cards.json', result)
    return result


def market():
    result = []
    for index in (1, 2):
        key, business = os.getenv(f'YANDEX_API_KEY_ACC{index}'), os.getenv(f'YANDEX_BUSINESS_ID_ACC{index}')
        if not key or not business:
            continue
        account = f'ya_acc{index}'
        h = {'Api-Key': key, 'Content-Type': 'application/json'}
        campaigns = read('https://api.partner.market.yandex.ru/v2/campaigns', h)
        save(f'{account}_campaigns.json', campaigns)
        assert not campaigns.get('paging', {}).get('nextPageToken')
        businesses = {str(business)} | {str(c['business']['id']) for c in campaigns.get('campaigns', [])}
        for biz in businesses:
            for archived in (False, True):
                token = None
                seen = set()
                for page in range(500):
                    params = {'limit': 200}
                    if token:
                        params['pageToken'] = token
                    data = read(f'https://api.partner.market.yandex.ru/v2/businesses/{biz}/offer-mappings',
                                h, {'archived': archived}, params)
                    save(f'raw/{account}_{biz}_{archived}_{page:03d}.json', data)
                    for c in data['result']['offerMappings']:
                        offer = c['offer']
                        if any(str(offer.get(k, '')).startswith('7108') for k in ('offerId', 'vendorCode')):
                            result.append(dict(platform='Маркет', account=account, business_id=biz,
                                               code=offer.get('vendorCode') or offer['offerId'], id=offer['offerId'],
                                               title=offer.get('name', ''), state='archive' if offer.get('archived') else 'active'))
                    token = data['result'].get('paging', {}).get('nextPageToken')
                    if not token:
                        break
                    if token in seen:
                        raise RuntimeError('Market cursor repeated')
                    seen.add(token)
                    time.sleep(.4)
                else:
                    raise RuntimeError('Market pagination incomplete')
        print(account, 'verified', sum(r['account'] == account for r in result), flush=True)
    save('market_cards.json', result)
    return result


def ms():
    result = {}
    for code in ('7108', '5292'):
        data = ms_api.get('/entity/product', {'filter': 'externalCode=' + code, 'limit': 20})
        assert data['meta']['size'] <= 20
        result[code] = data['rows']
    save('ms_live.json', result)
    return []


def main():
    OUT.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for fn in (wb, ozon, market, ms)]
        rows = [r for future in futures for r in future.result()]
    save('all_cards.json', rows)
    fields = ['platform', 'account', 'business_id', 'code', 'id', 'sku', 'state', 'title']
    with (OUT / 'all_cards.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=';')
        w.writeheader()
        w.writerows(rows)
    groups = []
    for account in sorted({r['account'] for r in rows}):
        subset = [r for r in rows if r['account'] == account]
        groups.append(dict(account=account, total=len(subset),
                           active=sum(r['state'] == 'active' for r in subset),
                           inactive=sum(r['state'] != 'active' for r in subset),
                           codes=sorted({r['code'] for r in subset})))
    summary = dict(at=datetime.now(timezone.utc).isoformat(), status='complete_read_only',
                   total=len(rows), groups=groups, external_writes=0, database_writes=0)
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
