"""Scheduled, guarded removal of Ozon automatic action enrollment (stream: mkt).

Default mode is read-only. --apply requires a Moscow-date guard, performs one
pilot per account, verifies all price fields, then processes bounded batches.
Per-item rejections are logged and retried once; malformed API responses or
price movement stop the run before the next batch.
"""

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from collectors.ozon_price_index import Api, PRICES_PATH
from ops import ozon_stock_action as oz
from ops.ozon_autoadd_flag_manifest import PRICE_FIELDS, build_manifest, save_manifest


ROOT = Path(__file__).resolve().parents[1]
WRITE_PATH = '/v1/product/import/prices'
LOG = Path('/opt/mp-analytics/logs/ozon_autoadd_night_runs.jsonl')
ATTEMPTS = Path('/opt/mp-analytics/logs/ozon_autoadd_write_attempts')
BATCH_SIZE = 50
VERIFY_DELAYS = (2, 3, 5, 8, 13, 21)
PRICE_COMPARE = ('price', 'old_price', 'min_price', 'currency_code',
                 'auto_action_enabled', 'declared_price', 'marketing_seller_price',
                 'net_price', 'retail_price', 'vat')
MSK = ZoneInfo('Europe/Moscow')


class StopRun(RuntimeError):
    pass


def _log(record):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(LOG, 0o600)


def _same(a, b):
    if a in (None, '') or b in (None, ''):
        return a in (None, '') and b in (None, '')
    if isinstance(a, (int, float, Decimal)) or isinstance(b, (int, float, Decimal)):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except InvalidOperation:
            pass
    return str(a) == str(b)


def _fetch_ids(api, product_ids):
    if not product_ids:
        return {}
    code, data = api.post(PRICES_PATH, {
        'filter': {'product_id': [int(value) for value in product_ids], 'visibility': 'ALL'},
        'limit': max(len(product_ids), 1),
    })
    if code != 200:
        raise StopRun(f'price reread HTTP {code}')
    items = data.get('items') or []
    found = {}
    for item in items:
        product_id = str(item.get('product_id') or '')
        if not product_id or product_id in found:
            raise StopRun('price reread has missing/duplicate product id')
        found[product_id] = item
    if len(found) != len(set(map(str, product_ids))):
        raise StopRun(f'price reread incomplete: expected {len(set(map(str, product_ids)))}, got {len(found)}')
    return found


def _snapshot(item):
    return {field: (item.get('price') or {}).get(field) for field in PRICE_FIELDS}


def _price_payload(item):
    price = item.get('price') or {}
    if not item.get('offer_id') or item.get('product_id') is None:
        raise StopRun('target lacks product_id or offer_id')
    if price.get('price') in (None, ''):
        raise StopRun('target lacks current price')
    body_price = {
        'product_id': int(item['product_id']),
        'offer_id': item['offer_id'],
        'price': str(price['price']),
        'auto_action_enabled': 'UNKNOWN',
        'auto_add_to_ozon_actions_list_enabled': 'DISABLED',
    }
    for source, dest in (('old_price', 'old_price'), ('min_price', 'min_price'),
                         ('currency_code', 'currency_code')):
        if price.get(source) not in (None, ''):
            body_price[dest] = str(price[source])
    return body_price


def _assert_unchanged(before, after):
    a, b = _snapshot(before), _snapshot(after)
    differences = [field for field in PRICE_COMPARE if not _same(a.get(field), b.get(field))]
    if a.get('auto_add_to_ozon_actions_list_enabled') is not True:
        raise StopRun('target flag is no longer true')
    if b.get('auto_add_to_ozon_actions_list_enabled') is not False:
        raise StopRun('target flag did not become false')
    if differences:
        raise StopRun('price fields changed: ' + ','.join(differences))


def _response_results(code, data, targets):
    if code != 200:
        raise StopRun(f'price import HTTP {code}')
    results = data.get('result')
    if not isinstance(results, list) or len(results) != len(targets):
        raise StopRun('price import result shape/count is unexpected')
    by_id = {}
    for item in results:
        product_id = str(item.get('product_id') or '')
        if not product_id or product_id in by_id:
            raise StopRun('price import result has missing/duplicate product id')
        by_id[product_id] = item
    if set(by_id) != {str(target['product_id']) for target in targets}:
        raise StopRun('price import result product ids do not match request')
    updated, rejected = [], []
    for target in targets:
        item = by_id[str(target['product_id'])]
        (updated if item.get('updated') is True and not item.get('errors') else rejected).append(target)
    return updated, rejected


def _price_problem(item):
    price = item.get('price') or {}
    if price.get('price') in (None, ''):
        return 'missing_price'
    if price.get('min_price') not in (None, ''):
        try:
            if Decimal(str(price['price'])) < Decimal(str(price['min_price'])):
                return 'price_below_min_price'
        except InvalidOperation:
            return 'invalid_price_number'
    return None


def _save_attempt(account, label, targets, body, code, data):
    # Persist the complete response BEFORE validation: Ozon may partially apply a batch.
    ATTEMPTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ATTEMPTS, 0o700)
    record = {'at': datetime.now(MSK).isoformat(), 'account': account, 'stage': label,
              'before': targets, 'request': body, 'http': code, 'response': data}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ATTEMPTS,
                                     prefix=f'{account}-{label}-', suffix='.json',
                                     delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
        path = stream.name
    _log({'at': record['at'], 'account': account, 'stage': 'write_response',
          'batch': label, 'count': len(targets), 'http': code, 'artifact': path})


def _wait_verify(api, before_items):
    ids = [str(item['product_id']) for item in before_items]
    last_error = None
    for delay in VERIFY_DELAYS:
        time.sleep(delay)
        try:
            after = _fetch_ids(api, ids)
            for before in before_items:
                _assert_unchanged(before, after[str(before['product_id'])])
            return
        except StopRun as exc:
            last_error = exc
            # A price change is an immediate stop; only a not-yet-visible flag is polled.
            if 'flag did not become false' not in str(exc):
                raise
    raise StopRun(f'flag verification timeout: {last_error}')


def _write_batch(api, items, label, retry=True):
    # Re-read each bounded batch immediately before it is written.
    latest_by_id = _fetch_ids(api, [str(item['product_id']) for item in items])
    targets = []
    for item in items:
        latest = latest_by_id[str(item['product_id'])]
        flag = (latest.get('price') or {}).get('auto_add_to_ozon_actions_list_enabled')
        if flag is True:
            problem = _price_problem(latest)
            if problem:
                _log({'at': datetime.now(MSK).isoformat(), 'account': api.account,
                      'stage': 'skipped_invalid_price', 'batch': label,
                      'product_id': latest['product_id'], 'reason': problem,
                      'price': (latest.get('price') or {}).get('price'),
                      'min_price': (latest.get('price') or {}).get('min_price')})
                continue
            targets.append(latest)
        elif flag is False:
            continue  # another process already removed it; nothing to write
        else:
            raise StopRun('unknown auto-add flag before write')
    if not targets:
        return 0
    body = {'prices': [_price_payload(item) for item in targets]}
    code, data = api.post(WRITE_PATH, body)
    _save_attempt(api.account, label, targets, body, code, data)
    updated, rejected = _response_results(code, data, targets)
    if updated:
        _wait_verify(api, updated)
        _log({'at': datetime.now(MSK).isoformat(), 'account': api.account,
              'stage': label, 'count': len(updated), 'status': 'verified'})
    if rejected:
        _log({'at': datetime.now(MSK).isoformat(), 'account': api.account,
              'stage': 'rejected', 'batch': label,
              'product_ids': [item['product_id'] for item in rejected]})
    changed = len(updated)
    if rejected and retry:
        for item in rejected:
            time.sleep(2)
            changed += _write_batch(api, [item], f'{label}-retry', retry=False)
    return changed


def apply(run_date):
    today = datetime.now(MSK).date().isoformat()
    if today != run_date:
        raise StopRun(f'run-date guard: expected {run_date}, actual {today}')
    manifest = build_manifest()
    manifest_path, digest = save_manifest(manifest)
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'preflight',
          'manifest': str(manifest_path), 'sha256': digest, 'rows': manifest['rows']})
    if any(row['unknown'] for row in manifest['rows']):
        raise StopRun('unknown flags in fresh complete scan')

    pending = {account: [row for row in manifest['targets'] if row['account'] == account]
               for account in oz.ACCOUNTS}
    apis = {account: Api(account) for account in oz.ACCOUNTS}
    # One guarded pilot per cabinet; do not mass-write unless both pass.
    pilot_count = 0
    for account in oz.ACCOUNTS:
        if not pending[account]:
            continue
        pilot = pending[account][0]
        item = _fetch_ids(apis[account], [str(pilot['product_id'])])[str(pilot['product_id'])]
        if (item.get('price') or {}).get('auto_add_to_ozon_actions_list_enabled') is not True:
            raise StopRun(f'{account}: pilot target changed before write')
        if _write_batch(apis[account], [item], 'pilot') != 1:
            raise StopRun(f'{account}: pilot did not verify')
        pilot_count += 1
        pending[account] = pending[account][1:]
        print(f'{account}: pilot verified; remaining={len(pending[account])}', flush=True)

    total = 0
    for account in oz.ACCOUNTS:
        queue = pending[account]
        for start in range(0, len(queue), BATCH_SIZE):
            batch_no = start // BATCH_SIZE + 1
            count = _write_batch(apis[account], queue[start:start + BATCH_SIZE],
                                 f'batch-{batch_no}')
            total += count
            if batch_no == 1 or batch_no % 10 == 0 or start + BATCH_SIZE >= len(queue):
                print(f'{account}: verified_batch={batch_no} changed={count} total={total}', flush=True)
    final = build_manifest()
    leftovers = sum(row['enabled'] for row in final['rows'])
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'final_scan',
          'enabled_remaining': leftovers, 'rows': final['rows']})
    for row in final['rows']:
        print(f"{row['account']}: enabled_remaining={row['enabled']} "
              f"unknown={row['unknown']}", flush=True)
    if any(row['unknown'] for row in final['rows']) or leftovers:
        raise StopRun(f'final verification incomplete: enabled={leftovers}')
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'complete',
          'changed': total + pilot_count,
          'enabled_remaining': leftovers, 'rows': final['rows']})
    print(f'complete: bulk_verified={total}; enabled_remaining={leftovers}', flush=True)


def pilot_only():
    """Write and verify exactly one eligible product in each Ozon account."""
    manifest = build_manifest()
    manifest_path, digest = save_manifest(manifest)
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'pilot_preflight',
          'manifest': str(manifest_path), 'sha256': digest, 'rows': manifest['rows']})
    if any(row['unknown'] for row in manifest['rows']):
        raise StopRun('unknown flags in fresh complete scan')

    apis = {account: Api(account) for account in oz.ACCOUNTS}
    selected = {}
    for account in oz.ACCOUNTS:
        candidates = [row for row in manifest['targets'] if row['account'] == account]
        if not candidates:
            raise StopRun(f'{account}: no enabled product available for pilot')
        for candidate in candidates:
            product_id = str(candidate['product_id'])
            item = _fetch_ids(apis[account], [product_id])[product_id]
            flag = (item.get('price') or {}).get('auto_add_to_ozon_actions_list_enabled')
            if flag is True:
                if _write_batch(apis[account], [item], 'pilot-only') != 1:
                    raise StopRun(f'{account}: pilot did not verify')
                selected[account] = product_id
                print(f'{account}: one pilot write verified; price fields unchanged', flush=True)
                break
            if flag is not False:
                raise StopRun(f'{account}: pilot flag is unknown before write')
        if account not in selected:
            raise StopRun(f'{account}: no enabled product remained for pilot')

    final = build_manifest()
    still_enabled = {(row['account'], str(row['product_id'])) for row in final['targets']}
    for account, product_id in selected.items():
        if (account, product_id) in still_enabled:
            raise StopRun(f'{account}: pilot still appears enabled in final scan')
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'pilot_complete',
          'changed': len(selected), 'enabled_remaining': final['rows']})
    for row in final['rows']:
        print(f"{row['account']}: enabled_remaining={row['enabled']} total={row['total']}",
              flush=True)
    print('pilot complete: 2 writes verified; no bulk writes', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Guarded Ozon auto-add flag removal')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='write Ozon flag after pilots')
    mode.add_argument('--pilot-only', action='store_true',
                      help='write and verify one product per account; no bulk writes')
    parser.add_argument('--run-date', help='required Moscow date YYYY-MM-DD for --apply')
    args = parser.parse_args(argv)
    if args.apply:
        if not args.run_date:
            parser.error('--apply requires --run-date')
        apply(args.run_date)
    elif args.pilot_only:
        if args.run_date:
            parser.error('--run-date applies only to --apply')
        pilot_only()
    else:
        manifest = build_manifest()
        print('read_only_preflight')
        for row in manifest['rows']:
            print(f"{row['account']}: enabled={row['enabled']} total={row['total']} "
                  f"unknown={row['unknown']}")
        print('Ozon writes=0')


if __name__ == '__main__':
    try:
        main()
    except StopRun as exc:
        _log({'at': datetime.now(MSK).isoformat(), 'stage': 'stopped',
              'reason': str(exc)[:220]})
        print(f'stopped safely: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
    except Exception as exc:
        _log({'at': datetime.now(MSK).isoformat(), 'stage': 'failed',
              'error_type': type(exc).__name__})
        print(f'stopped safely: unexpected {type(exc).__name__}', file=sys.stderr, flush=True)
        raise SystemExit(2)
