# поток: inv
"""Thirty-minute FBS-to-MS customerorder poller using the recovery importer.

Only --apply can POST customerorders. A durable cursor advances only after all
eligible missing orders in a source window are resolved. No marketplace writes.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

from core import ms_api
from ops.order_priority import order_key
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_read import find_customerorder, product_cards
from ops.fbs_ms_set_catalog import catalog as set_catalog, components as set_components
from ops.fbs_ms_import_wb_pilot import (current_purchase_documents,
                                     purchase_documents_digest, current_purchase_prices)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import'
RUNTIME = OUT / 'schedule'
STATE = RUNTIME / 'state.json'
LOCK = RUNTIME / 'lock'
KNOWN = RUNTIME / 'known_ms.json'
BASE = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
MAX_PER_RUN = 10
OVERLAP = timedelta(hours=2)


def stamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('naive_cursor')
    return result.astimezone(timezone.utc)


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + '.tmp')
    save(temp, data)
    os.replace(temp, path)


def cursor() -> datetime:
    if STATE.exists():
        return stamp(json.loads(STATE.read_text())['cursor_utc'])
    final = OUT / 'final_verification.json'
    if final.exists():
        return stamp(json.loads(final.read_text())['cutoff_utc'])
    return BASE


def permitted(row: dict) -> bool:
    platform, status = row['platform'], row['status']
    return ((platform == 'ozon' and status == 'awaiting_packaging') or
            (platform == 'wb' and status == ['new', 'waiting']) or
            (platform == 'yandex' and status in
             (['PROCESSING', 'STARTED'], ['UNPAID', 'WAITING_USER_INPUT'])))


def cancelled(row: dict) -> bool:
    status = row['status']
    return (row['platform'] == 'ozon' and status == 'cancelled' or
            row['platform'] == 'yandex' and isinstance(status, list) and bool(status) and status[0] == 'CANCELLED' or
            row['platform'] == 'wb' and isinstance(status, list) and len(status) == 2 and
            (status[0] in ('cancel', 'cancelled') or
             status[1] in ('declined_by_client', 'canceled_by_client', 'cancelled')))


def recoverable_import_race(exc: Exception) -> str | None:
    """Only a proven pre-POST marketplace/staff race may unblock the cursor."""
    reason = str(exc)
    if reason == 'already_in_ms':
        return 'staff_created'
    if reason in ('ozon_status_changed', 'wb_status_changed', 'yandex_status_changed'):
        return 'market_status_changed'
    return None


def known_ms_numbers() -> set[str]:
    known = set(json.loads(KNOWN.read_text())['numbers']) if KNOWN.exists() else set()
    # Seed the scheduled ledger from already API-confirmed recovery artifacts.
    for name in ('reconcile.json', 'reconcile_latest.json'):
        path = OUT / name
        if path.exists():
            known.update(json.loads(path.read_text()).get('ms_found', {}))
    for path in OUT.glob('next5_*/readback.json'):
        known.add(json.loads(path.read_text())['name'])
    return known


def exact_ms_numbers(rows: list[dict]) -> set[str]:
    names = [r['number'] for r in rows]
    if len(set(names)) != len(names):
        raise RuntimeError('source_duplicate_number')
    found: set[str] = set()
    for start in range(0, len(names), 15):
        part = names[start:start + 15]
        data = ms_api.get('/entity/customerorder',
                          {'limit': 100, 'filter': ['name=' + n for n in part]})
        hits = data.get('rows') or []
        if data.get('meta', {}).get('size') != len(hits):
            raise RuntimeError('ms_search_incomplete')
        for order in hits:
            number = order.get('name')
            if number not in part:
                continue
            if number in found:
                raise RuntimeError('duplicate_ms_number:' + number)
            found.add(number)
    return found


def enter_digest() -> str:
    return purchase_documents_digest(current_purchase_documents())


def prices_for(rows: list[dict]) -> tuple[dict[str, int], str]:
    codes = set()
    direct_codes = set()
    try:
        sets = set_catalog()
    except Exception:
        # Ordinary products can still use the existing current-Enter price path.
        sets = {}
    for row in rows:
        for product in row.get('products') or []:
            code = product.get('code')
            if not isinstance(code, str) or not re.fullmatch(r'\d{4}.*', code):
                raise RuntimeError('market_code_not_four_digit_family:' + row['number'])
            codes.add(code[:4])
            direct_codes.add(code[:4])
            try:
                kit = set_components(code[:4], sets)
            except ValueError:
                kit = None
            if kit:
                codes.update(component for component, _ in kit)
    digest = enter_digest()
    path = OUT / 'prices_snapshot_all.json'
    cached = json.loads(path.read_text()) if path.exists() else {'prices': {}, 'codes': [], 'digest': ''}
    if cached['digest'] != digest:
        cached = {'prices': {}, 'codes': [], 'digest': digest, 'complete': False}
    new_codes = codes - set(cached['codes'])
    if new_codes:
        by_code = {code: product_cards(code) for code in sorted(new_codes)}
        missing_cards = [code for code, cards in by_code.items()
                         if not cards and code in direct_codes and code not in sets]
        if missing_cards:
            raise RuntimeError('exact_base_product_card_missing:' + ','.join(missing_cards))
        cached['codes'] = sorted(set(cached['codes']) | new_codes)
    if not cached.get('complete'):
        # The Enter scan already reads every position. Cache every unambiguous
        # current price once per document digest, not one full scan per new SKU.
        prices, scanned_digest = current_purchase_prices(None)
        # The reader validates its final receipt generation and may have followed
        # a supplier replacement since our initial cache lookup.
        digest = scanned_digest
        cached['digest'] = digest
        cached['prices'] = prices
        cached['complete'] = True
    atomic_save(path, cached)
    return cached['prices'], digest


def source_rows() -> tuple[list[dict], str, str]:
    start = max(BASE, cursor() - OVERLAP)
    os.environ['FBS_SCAN_START_UTC'] = start.isoformat()
    from ops import fbs_ms_source_scan as scanner
    summary = scanner.scan()
    snapshot = json.loads((OUT / 'source_scan.json').read_text())
    for account in sorted(summary['errors']):
        for attempt in range(2):
            time.sleep(3 * (attempt + 1))
            try:
                end_time = stamp(snapshot['window_utc'][1])
                rows = (scanner.yandex(end_time) if account == 'ya_acc1' else
                        scanner.ozon(account, end_time) if account.startswith('oz_') else
                        scanner.wb(account, end_time))
                snapshot['accounts'][account] = rows
                snapshot['errors'].pop(account, None)
                break
            except Exception as exc:
                snapshot['errors'][account] = type(exc).__name__ + ':' + str(exc)[:100]
    atomic_save(OUT / 'source_scan.json', snapshot)
    rows = [r for batch in snapshot['accounts'].values() for r in batch]
    rows.sort(key=order_key)
    return rows, start.isoformat(), snapshot['window_utc'][1]


def run(apply: bool) -> dict:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('scheduler_already_running') from None
    previous_limits = ms_api.TIMEOUT, ms_api.RETRIES
    ms_api.TIMEOUT, ms_api.RETRIES = 45, 2
    try:
        rows, start, end = source_rows()
        snapshot_path = OUT / 'source_scan.json'
        scan_errors = json.loads(snapshot_path.read_text()).get('errors', {}) if snapshot_path.exists() else {}
        known = known_ms_numbers()
        unknown = [r for r in rows if r['number'] not in known and not cancelled(r)]
        found = exact_ms_numbers(unknown)
        known.update(found)
        missing = [r for r in unknown if r['number'] not in found]
        eligible = sorted((r for r in missing if permitted(r)),key=order_key)
        held = [r for r in missing if not permitted(r) and not cancelled(r)]
        result = {'window_utc': [start, end], 'source': len(rows),
                  'already_ms': sum(r['number'] in known for r in rows),
                  'eligible': len(eligible), 'held': [r['number'] for r in held],
                  'cancelled': sum(cancelled(r) for r in rows if r['number'] not in known),
                  'created': [], 'skipped_staff': [], 'skipped_changed': [],
                  'errors': [{'account': account, 'reason': 'source_scan_failed', 'detail': detail}
                             for account, detail in sorted(scan_errors.items())]}
        if not apply:
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        batch = eligible[:MAX_PER_RUN]
        if batch:
            prices, digest = prices_for(batch)
            if enter_digest() != digest:
                raise RuntimeError('enter_changed_before_batch')
            from ops import fbs_ms_import_next5 as importer
            accounts = {r['account'].removeprefix('wb_') for r in batch if r['platform'] == 'wb'}
            stats = {account: importer.wb_statistics(account) for account in sorted(accounts)}
            promised = defaultdict(Decimal)
            for row in batch:
                number = row['number']
                stop_batch = False
                try:
                    if find_customerorder(number):
                        result['skipped_staff'].append(number)
                        known.add(number)
                    else:
                        created = importer.create_one(row, stats, prices, digest, promised)
                        result['created'].append(created)
                        known.add(number)
                        print(json.dumps(created, ensure_ascii=False), flush=True)
                except Exception as exc:
                    reason = str(exc)
                    race = recoverable_import_race(exc)
                    if race == 'staff_created':
                        result['skipped_staff'].append(number)
                        known.add(number)
                    elif race == 'market_status_changed':
                        result['skipped_changed'].append(number)
                    else:
                        result['errors'].append({'number': number,
                                                 'reason': type(exc).__name__ + ':' + reason[:160]})
                        print(json.dumps(result['errors'][-1], ensure_ascii=False), flush=True)
                        stop_batch = (OUT / ('next5_' + number) / 'intent.json').exists()
                atomic_save(RUNTIME / 'latest.json', result)
                atomic_save(KNOWN, {'numbers': sorted(known)})
                if stop_batch:
                    break
        if not result['errors'] and len(eligible) <= MAX_PER_RUN:
            atomic_save(STATE, {'cursor_utc': end, 'completed_at_utc': datetime.now(timezone.utc).isoformat()})
            result['cursor_advanced'] = True
        else:
            result['cursor_advanced'] = False
        atomic_save(RUNTIME / 'latest.json', result)
        atomic_save(KNOWN, {'numbers': sorted(known)})
        return result
    finally:
        ms_api.TIMEOUT, ms_api.RETRIES = previous_limits
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    summary = run(args.apply)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('created',)}, ensure_ascii=False))
    if summary['errors']:
        sys.exit(1)
