# поток: mkt
"""One-off repair of the 23 Ozon auto-add flags skipped on 30 October 2026.

For each exact skipped product, lower the inconsistent min_price to its freshly
read current price and disable auto-add. No other product or price is targeted.
"""

import argparse
from datetime import datetime
from decimal import Decimal
import json
import sys
import time

from collectors.ozon_price_index import Api
from ops.ozon_autoadd_flag_manifest import build_manifest
from ops.ozon_autoadd_flag_night import (
    LOG, MSK, PRICE_COMPARE, StopRun, VERIFY_DELAYS, WRITE_PATH,
    _fetch_ids, _log, _price_payload, _response_results, _same,
    _save_attempt, _snapshot,
)


EXPECTED = {'oz_acc1': 12, 'oz_acc2': 11}
SOURCE_PREFIX = '2026-10-30T21:'


def _targets():
    records = [json.loads(line) for line in LOG.read_text(encoding='utf-8').splitlines()]
    targets = {}
    for account, expected in EXPECTED.items():
        ids = [str(record['product_id']) for record in records
               if record.get('stage') == 'skipped_invalid_price'
               and record.get('account') == account
               and record.get('at', '').startswith(SOURCE_PREFIX)]
        if len(ids) != expected or len(set(ids)) != expected:
            raise StopRun(f'{account}: expected {expected} unique skipped products, got {len(ids)}')
        targets[account] = ids
    return targets


def _verify(api, before):
    product_id = str(before['product_id'])
    original = _snapshot(before)
    for delay in VERIFY_DELAYS:
        time.sleep(delay)
        after = _fetch_ids(api, [product_id])[product_id]
        current = _snapshot(after)
        changed = [field for field in PRICE_COMPARE
                   if field != 'min_price' and not _same(original.get(field), current.get(field))]
        if changed:
            raise StopRun(f'{api.account} product_id={product_id}: other price fields changed: {changed}')
        if (current['auto_add_to_ozon_actions_list_enabled'] is False
                and _same(current['min_price'], original['price'])):
            return
    raise StopRun(f'{api.account} product_id={product_id}: flag/min_price verification timed out')


def apply(run_date):
    if datetime.now(MSK).date().isoformat() != run_date:
        raise StopRun(f'run-date guard failed for {run_date}')
    targets = _targets()
    changed = {}
    for account, ids in targets.items():
        api = Api(account)
        changed[account] = 0
        for product_id in ids:
            before = _fetch_ids(api, [product_id])[product_id]
            price = before.get('price') or {}
            flag = price.get('auto_add_to_ozon_actions_list_enabled')
            if flag is False:
                _log({'at': datetime.now(MSK).isoformat(), 'stage': 'min_fix_already_disabled',
                      'account': account, 'product_id': product_id})
                continue
            if flag is not True or price.get('price') in (None, '') or price.get('min_price') in (None, ''):
                raise StopRun(f'{account} product_id={product_id}: missing flag or price')
            if Decimal(str(price['price'])) >= Decimal(str(price['min_price'])):
                raise StopRun(f'{account} product_id={product_id}: price is no longer below minimum')
            body_price = _price_payload(before)
            body_price['min_price'] = str(price['price'])
            code, data = api.post(WRITE_PATH, {'prices': [body_price]})
            _save_attempt(account, 'min-fix', [before], {'prices': [body_price]}, code, data)
            updated, rejected = _response_results(code, data, [before])
            if rejected or len(updated) != 1:
                raise StopRun(f'{account} product_id={product_id}: Ozon rejected min-price repair')
            _verify(api, before)
            changed[account] += 1
            _log({'at': datetime.now(MSK).isoformat(), 'stage': 'min_fix_verified',
                  'account': account, 'product_id': product_id,
                  'old_min_price': price['min_price'], 'new_min_price': price['price']})
            print(f'{account}: verified {changed[account]}/{len(ids)}', flush=True)

    final = build_manifest()
    _log({'at': datetime.now(MSK).isoformat(), 'stage': 'min_fix_final_scan',
          'changed': changed, 'rows': final['rows']})
    for row in final['rows']:
        print(f"{row['account']}: enabled_remaining={row['enabled']} unknown={row['unknown']}", flush=True)
    if any(row['unknown'] or row['enabled'] for row in final['rows']):
        raise StopRun('final scan still has enabled or unknown auto-add flags')
    print('min-price repair complete', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Repair exactly 23 skipped Ozon auto-add flags')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--run-date')
    args = parser.parse_args()
    if not args.apply or not args.run_date:
        parser.error('writes require --apply and --run-date')
    apply(args.run_date)


if __name__ == '__main__':
    try:
        main()
    except StopRun as exc:
        _log({'at': datetime.now(MSK).isoformat(), 'stage': 'min_fix_stopped',
              'reason': str(exc)[:220]})
        print(f'stopped safely: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
