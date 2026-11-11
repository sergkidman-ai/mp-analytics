# поток: mkt / WB read-only audit
"""Use existing WB read helpers for one bounded audit; no DB or marketplace mutations."""
import argparse
import collections
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
import time

ROOT = Path('/opt/mp-analytics')
sys.path.insert(0, str(ROOT))
import requests
from collectors import wb_ads, wb_card_content, wb
from collectors.mp_fbo_orders import _wb_request, WB_ORDERS_URL
from ops.wb_stock_guard import live_stock, V4

P = Path(__file__).parent
parser = argparse.ArgumentParser()
parser.add_argument('scope', choices=['cards', 'ads', 'orders', 'fbs', 'fbo'])
args = parser.parse_args()
RAW = P / 'raw' / args.scope
RAW.mkdir(parents=True, exist_ok=True)
original = requests.sessions.Session.request
seq = max([int(f.stem.split('_')[-1]) for f in RAW.glob('read_*.json')] or [0])
last_content = 0
consecutive429 = 0
content_cache = {}
if args.scope == 'cards':
    for file in sorted(RAW.glob('read_*.json')):
        z = json.loads(file.read_text())
        if z['http'] == 200 and z['url'] == wb_card_content.CARDS_URL:
            content_cache[json.dumps(z['request'], sort_keys=True)] = z['response']


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, obj):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + '\n')
    tmp.replace(path)


def guard(self, method, url, **kw):
    global seq, last_content, consecutive429
    method = method.upper()
    allowed = method == 'GET' and (url == V4 or url == WB_ORDERS_URL or url in [
        wb_ads.B + '/adv/v1/promotion/count', wb_ads.B + '/api/advert/v2/adverts',
        wb_ads.B + '/adv/v3/fullstats'])
    allowed |= method == 'POST' and url == wb_card_content.CARDS_URL
    allowed |= method == 'GET' and (url.startswith(wb.REMAINS_URL) or url == 'https://marketplace-api.wildberries.ru/api/v3/warehouses')
    allowed |= method == 'POST' and url.startswith('https://marketplace-api.wildberries.ru/api/v3/stocks/')
    assert allowed, 'Read-only WB audit endpoint allowlist'
    if url == wb_card_content.CARDS_URL:
        cached = content_cache.get(json.dumps(kw.get('json'), sort_keys=True))
        if cached is not None:
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(cached, ensure_ascii=False).encode()
            response.encoding = 'utf-8'
            return response
        time.sleep(max(0, .65 - (time.monotonic() - last_content)))
        last_content = time.monotonic()
    response = original(self, method, url, **kw)
    seq += 1
    try:
        payload = response.json()
    except ValueError:
        payload = {'non_json_body': response.text[:1000]}
    save(RAW / f'read_{seq:05}.json', {'account': 'wb_acc1', 'at_utc': now(), 'method': method,
         'url': url, 'params': kw.get('params'), 'request': kw.get('json'),
         'http': response.status_code, 'response': payload})
    with (P / (args.scope + '_reads.jsonl')).open('a') as f:
        f.write(json.dumps({'account': 'wb_acc1', 'at_utc': now(), 'method': method, 'url': url,
                            'http': response.status_code, 'raw_file': f'raw/{args.scope}/read_{seq:05}.json'}) + '\n')
    consecutive429 = consecutive429 + 1 if response.status_code == 429 else 0
    assert consecutive429 < 6, 'Bounded read rate-limit budget exhausted'
    return response


requests.sessions.Session.request = guard
if args.scope == 'cards':
    index_path = P / 'cards_index.json'
    if index_path.exists():
        index = json.loads(index_path.read_text())['cards']
    else:
        # Existing read-only Content fetch; do not call main/load_raw, which write DB.
        with (P / 'cards_fetch_progress.log').open('a') as log, contextlib.redirect_stdout(log):
            cards = wb_card_content.fetch('wb_acc1')
        assert cards and len(cards) == len({c['nmID'] for c in cards})
        index = [{k: c.get(k) for k in ['nmID', 'imtID', 'vendorCode', 'title', 'brand', 'subjectID', 'createdAt', 'updatedAt', 'sizes']}
                 for c in cards]
        save(index_path, {'account': 'wb_acc1', 'at_utc': now(), 'source': 'existing wb_card_content.fetch, active catalog only', 'cards': index})
        print('Active content catalog verified', len(index), flush=True)
    print('Content ready; public stock HTTP403 is unknown, official FBS/FBO stock reads used instead', flush=True)

elif args.scope == 'fbo':
    h = {'Authorization': wb._token('wb_acc1')}
    response = requests.get(wb.REMAINS_URL, headers=h, params={'groupByNm': 'true', 'groupBySa': 'true'}, timeout=120)
    response.raise_for_status()
    task = response.json()['data']['taskId']
    save(P / 'fbo_stock_task.json', {'account': 'wb_acc1', 'at_utc': now(), 'taskId': task})
    for attempt in range(40):
        time.sleep(6)
        response = requests.get(f'{wb.REMAINS_URL}/tasks/{task}/status', headers=h, timeout=60)
        if response.status_code == 429:
            continue
        response.raise_for_status()
        if response.json().get('data', {}).get('status') == 'done':
            break
    else:
        raise RuntimeError('FBO warehouse remains task not complete')
    response = requests.get(f'{wb.REMAINS_URL}/tasks/{task}/download', headers=h, timeout=180)
    response.raise_for_status()
    save(P / 'fbo_stock.json', {'account': 'wb_acc1', 'at_utc': now(), 'complete': True, 'response': response.json()})
    print('Official FBO remains downloaded', len(response.json()), 'rows', flush=True)

elif args.scope == 'fbs':
    cards = json.loads((P / 'cards_index.json').read_text())['cards']
    skus = sorted({str(sku) for c in cards for size in c.get('sizes') or [] for sku in size.get('skus') or []})
    h = {'Authorization': wb._token('wb_acc1')}
    response = requests.get('https://marketplace-api.wildberries.ru/api/v3/warehouses', headers=h, timeout=60)
    response.raise_for_status()
    warehouses = response.json()
    assert isinstance(warehouses, list)
    save(P / 'seller_warehouses.json', {'account': 'wb_acc1', 'at_utc': now(), 'warehouses': warehouses})
    for wh in warehouses:
        if wh.get('isDeleting'):
            continue
        stock = []
        for i in range(0, len(skus), 1000):
            file = P / f'fbs_{wh["id"]}_{i // 1000:03}.json'
            if file.exists():
                data = json.loads(file.read_text())['response']
            else:
                response = requests.post(f'https://marketplace-api.wildberries.ru/api/v3/stocks/{wh["id"]}', headers=h,
                                         json={'skus': skus[i:i + 1000]}, timeout=90)
                response.raise_for_status()
                data = response.json()
                save(file, {'account': 'wb_acc1', 'at_utc': now(), 'warehouse_id': wh['id'],
                     'requested_skus': skus[i:i + 1000], 'response': data})
                time.sleep(.25)
            stock.extend(data.get('stocks') or [])
        print('Official FBS warehouse', wh['id'], 'requested', len(skus), 'returned', len(stock), flush=True)

elif args.scope == 'orders':
    # Reuse the existing complete Statistics reader with a narrowed lastChangeDate boundary.
    data = _wb_request('GET', WB_ORDERS_URL, 'wb_acc1', params={'dateFrom': '2026-10-12T00:00:00', 'flag': 0})
    assert isinstance(data, list) and len(data) < 80000, 'Pagination or malformed general orders response requires review'
    save(P / 'general_orders_raw.json', {'account': 'wb_acc1', 'at_utc': now(), 'last_change_from_msk': '2026-10-12T00:00:00',
         'order_date_analysis_from_msk': '2026-10-12', 'order_date_analysis_through_msk': '2026-11-08',
         'complete_below_page_limit': True, 'orders': data})
    print('General orders read', len(data), 'including updated older orders; analysis filters order date', flush=True)

else:
    h = {'Authorization': wb_ads._token('wb_acc1')}
    count = json.loads((P / 'raw/campaign_count.json').read_text())['response']
    meta = {}
    for group in count.get('adverts') or []:
        for c in group.get('advert_list') or []:
            meta[int(c['advertId'])] = {'id': int(c['advertId']), 'type': group['type'], 'status': group['status'], 'change_time': c.get('changeTime')}
    current_ids = [cid for cid, c in meta.items() if c['status'] in [4, 9, 11]]
    details = []
    if (P / 'current_campaigns.json').exists():
        details = json.loads((P / 'current_campaigns.json').read_text())['campaigns']
    else:
        for i in range(0, len(current_ids), 50):
            response = wb_ads._get(h, '/api/advert/v2/adverts', ids=','.join(map(str, current_ids[i:i + 50])))
            response.raise_for_status()
            details.extend(response.json().get('adverts') or [])
            time.sleep(.35)
    assert set(current_ids) == {int(c['id']) for c in details}, 'Current composition coverage incomplete'
    save(P / 'current_campaigns.json', {'account': 'wb_acc1', 'at_utc': now(), 'campaigns': details, 'count_source': meta})
    print('Current complete campaign compositions', len(details), 'payment types',
          dict(collections.Counter((c.get('settings') or {}).get('payment_type') for c in details)), flush=True)
    stats_ids = [cid for cid, c in meta.items() if c['status'] in [9, 11] or
                 (c['status'] == 7 and (c['change_time'] or '')[:10] >= '2026-10-12')]
    save(P / 'stats_scope.json', {'account': 'wb_acc1', 'from_msk': '2026-10-12', 'through_msk': '2026-11-08',
         'campaign_ids': stats_ids, 'includes_recently_finished': True})
    last_stats = [0.0]
    def get_stats(ids, retry=0):
        time.sleep(max(0, 11 - (time.monotonic() - last_stats[0])))
        last_stats[0] = time.monotonic()
        response = wb_ads._get(h, '/adv/v3/fullstats', ids=','.join(map(str, ids)), beginDate='2026-10-12', endDate='2026-11-08')
        if response.status_code in [500, 502, 503, 504] and retry < 2:
            return get_stats(ids, retry + 1)
        if response.status_code in [500, 502, 503, 504] and len(ids) > 1:
            mid = len(ids) // 2
            return get_stats(ids[:mid]) + get_stats(ids[mid:])
        response.raise_for_status()
        data = response.json()
        assert isinstance(data, list)
        return data
    for i in range(0, len(stats_ids), wb_ads.BATCH):
        file = P / f'fullstats_batch_{i // wb_ads.BATCH:02}.json'
        if file.exists():
            continue
        data = get_stats(stats_ids[i:i + wb_ads.BATCH])
        save(file, {'account': 'wb_acc1', 'at_utc': now(), 'requested_campaign_ids': stats_ids[i:i + wb_ads.BATCH],
                    'from_msk': '2026-10-12', 'through_msk': '2026-11-08', 'response': data})
        print('Bounded 28-day ads batch', i // wb_ads.BATCH + 1, 'campaigns', len(data), flush=True)
    print('WB audit reads complete; no bids/budgets/campaign changes or DB writes', flush=True)
