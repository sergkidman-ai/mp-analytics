# поток: inv
"""One-time, bounded MS status reconciliation for Sep-Nov 2026 FBS orders.

Only existing MS orders in Собирается/В пути are candidates. No marketplace writes.
Preview reads statuses in batches; apply re-reads both sides before each MS PUT.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

from dotenv import load_dotenv
import requests

from core import ms_api
from customer_guard.sources import get_posting
from customer_guard.wb import WBClient, TRANSIT as WB_TRANSIT
from customer_guard.yandex import YandexClient
from shipment_guard import config

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
OUT = ROOT / 'docs/reports/ms_market_status_sync_20261102'
FROM = '2026-09-01 00:00:00'
TO = '2026-12-01 00:00:00'
SOURCE_STATES = ('Собирается', 'В пути')
TARGET_STATES = ('В пути', 'Доставлен', 'Отменен - сроки')
MAX_CANDIDATES = 1500
MAX_WRITES = 3000
MIN_WRITE_INTERVAL = 1.25


def journal(kind: str, data: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    path = OUT / 'journal.jsonl'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as stream:
        stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(),
                                 'kind': kind, **data}, ensure_ascii=False, default=str) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def state_href(uid: str) -> str:
    return ms_api.BASE + '/entity/customerorder/metadata/states/' + uid


def state_ids() -> dict[str, str]:
    rows = ms_api.get('/entity/customerorder/metadata').get('states') or []
    found = defaultdict(list)
    for row in rows:
        if row.get('name') in SOURCE_STATES + TARGET_STATES:
            found[row['name']].append(row['id'])
    if any(len(found[name]) != 1 for name in SOURCE_STATES + TARGET_STATES):
        raise RuntimeError('ms_status_metadata_ambiguous')
    return {key: value[0] for key, value in found.items()}


def operational_ready() -> None:
    """The existing FBS poller must be healthy before adding webhook traffic."""
    def systemd(*args: str) -> str:
        result = subprocess.run(('systemctl', *args), capture_output=True, text=True,
                                timeout=10, check=False)
        if result.returncode:
            raise RuntimeError('fbs_poller_systemd_unavailable')
        return result.stdout.strip()

    if systemd('is-active', 'fbs-ms-import.timer') != 'active':
        raise RuntimeError('fbs_import_timer_inactive')
    if systemd('show', '-p', 'Result', '--value', 'fbs-ms-import.service') != 'success':
        raise RuntimeError('fbs_import_last_run_failed')
    ms_api.get('/context/employee')


def identity(order: dict) -> tuple[str, str | None]:
    platform, account, _, _ = config.identity(order)
    return platform, account


def source_state(order: dict) -> str:
    return order.get('state', {}).get('name') or ''


def candidates(states: dict[str, str]) -> tuple[list[dict], Counter]:
    rows, excluded = [], Counter()
    seen = set()
    for state in SOURCE_STATES:
        flt = f'state={state_href(states[state])};moment>={FROM};moment<{TO}'
        for order in ms_api.iter_rows('/entity/customerorder', {'filter': flt,
                                                                  'expand': 'state'}, page=100):
            if order.get('id') in seen:
                raise RuntimeError('duplicate_ms_id')
            seen.add(order.get('id'))
            if len(seen) > MAX_CANDIDATES:
                raise RuntimeError('candidate_limit_exceeded')
            platform, account = identity(order)
            if (platform, account) not in {('wb', 'wb_acc1'), ('wb', 'wb_acc2'),
                                           ('ozon', 'oz_acc1'), ('ozon', 'oz_acc2'),
                                           ('yandex', 'ya_acc1')}:
                excluded['unsupported_or_direct'] += 1
                continue
            if source_state(order) != state or not FROM[:10] <= str(order.get('moment') or '')[:10] < TO[:10]:
                excluded['ms_filter_mismatch'] += 1
                continue
            rows.append({'id': order['id'], 'number': order['name'], 'state': state,
                         'moment': order['moment'], 'platform': platform, 'account': account,
                         'updated': order.get('updated')})
    return rows, excluded


def marketplace_status(row: dict) -> tuple[str, str]:
    account, number = row['account'], row['number']
    if row['platform'] == 'ozon':
        if not re.fullmatch(r'\d+-\d+-\d+', number):
            raise ValueError('invalid_ozon_number')
        post = get_posting(number, account)
        status = post.get('status')
        return ({'delivering': 'transit', 'delivered': 'delivered',
                 'cancelled': 'cancelled'}.get(status, 'other'), str(status))
    if row['platform'] == 'wb':
        if not number.isdecimal():
            raise ValueError('invalid_wb_number')
        state = WBClient(account=account, budget=1).statuses([int(number)])[int(number)]
        return interpret_wb(state)
    if not number.isdecimal():
        raise ValueError('invalid_yandex_number')
    data = YandexClient(budget=1).call({'orderIds': [int(number)]})['orders']
    if len(data) != 1 or str(data[0].get('orderId')) != number:
        raise ValueError('yandex_order_identity_mismatch')
    return interpret_yandex(data[0])


def interpret_wb(state: dict) -> tuple[str, str]:
    supplier, wb = state.get('supplierStatus'), state.get('wbStatus')
    if wb in {'canceled', 'canceled_by_client', 'declined_by_client', 'defect',
              'canceled_by_carrier'} or supplier in {'cancel', 'cancel_carrier'}:
        return 'cancelled', f'{supplier}:{wb}'
    if wb == 'sold':
        return 'delivered', f'{supplier}:{wb}'
    return ('transit' if wb in WB_TRANSIT else 'other'), f'{supplier}:{wb}'


def interpret_yandex(order: dict) -> tuple[str, str]:
    if order.get('programType') != 'FBS' or order.get('fake') is not False:
        raise ValueError('yandex_not_real_fbs')
    status = order.get('status')
    return ({'DELIVERY': 'transit', 'PICKUP': 'transit', 'DELIVERED': 'delivered',
             'CANCELLED': 'cancelled'}.get(status, 'other'), f'{status}:{order.get("substatus")}' )


def preview_market(rows: list[dict]) -> dict[tuple[str, str], tuple[str, str]]:
    """Batch read status APIs where they support IDs; Ozon uses exact posting GET."""
    result = {}
    groups = defaultdict(list)
    for row in rows:
        groups[(row['platform'], row['account'])].append(row)
    for (platform, account), items in groups.items():
        if platform == 'wb':
            ids = [int(row['number']) for row in items]
            states = WBClient(account=account, budget=max(1, (len(ids)+99)//100)).statuses(ids)
            for row in items:
                result[(account, row['number'])] = interpret_wb(states[int(row['number'])])
        elif platform == 'yandex':
            client = YandexClient(budget=max(1, (len(items)+19)//20))
            for start in range(0, len(items), 20):
                part = items[start:start+20]
                ids = [int(row['number']) for row in part]
                data = client.call({'orderIds': ids})['orders']
                returned = [x.get('orderId') for x in data]
                if len(returned) != len(set(returned)) or not set(returned) <= set(ids):
                    raise ValueError('yandex_batch_identity_mismatch')
                for order in data:
                    result[(account, str(order['orderId']))] = interpret_yandex(order)
                for missing in set(ids) - set(returned):
                    result[(account, str(missing))] = (None, 'yandex_order_not_returned')
    return result


def target(current: str, market: str) -> str | None:
    if current not in SOURCE_STATES:
        return None
    if market == 'cancelled':
        return 'Отменен - сроки'
    if market == 'delivered':
        return 'Доставлен'
    if market == 'transit' and current == 'Собирается':
        return 'В пути'
    return None


def fresh_order(row: dict) -> dict:
    order = ms_api.get('/entity/customerorder/' + row['id'], {'expand': 'state'})
    if (order.get('id') != row['id'] or order.get('name') != row['number'] or
            identity(order) != (row['platform'], row['account']) or
            str(order.get('moment') or '')[:10] != row['moment'][:10]):
        raise RuntimeError('ms_order_identity_changed')
    return order


def apply_one(row: dict, states: dict[str, str]) -> str:
    order = fresh_order(row)
    before = source_state(order)
    intent_path = OUT / 'intents' / (row['id'] + '.json')
    if intent_path.exists():
        prior = json.loads(intent_path.read_text())
        if before == prior.get('target'):
            journal('prior_intent_confirmed', {**row, 'target': before})
            return 'already_updated'
        raise RuntimeError('prior_status_intent_requires_reconciliation:' + row['number'])
    if before != row['state']:
        journal('skip_changed', {**row, 'current': before})
        return 'changed_by_other'
    try:
        market, detail = marketplace_status(row)
    except Exception as exc:
        journal('market_unverified', {**row, 'error': type(exc).__name__,
                                      'reason': str(exc)[:120]})
        return 'market_unverified'
    desired = target(before, market)
    if desired is None:
        journal('skip_no_transition', {**row, 'market': market, 'detail': detail})
        return 'unchanged'
    # Fresh MS read immediately before the one-shot PUT, closing most staff races.
    latest = fresh_order(row)
    if latest.get('updated') != order.get('updated') or source_state(latest) != before:
        journal('skip_race', {**row, 'current': source_state(latest)})
        return 'changed_by_other'
    payload = {'state': {'meta': {'href': state_href(states[desired]),
                                  'type': 'state', 'mediaType': 'application/json'}}}
    intent_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    intent_path.parent.chmod(0o700)
    fd = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'number': row['number'], 'before': before, 'target': desired,
                   'ms_updated': latest.get('updated'), 'market': market,
                   'detail': detail}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    journal('intent', {**row, 'market': market, 'detail': detail,
                       'before': before, 'target': desired, 'ms_updated': latest.get('updated')})
    try:
        response = requests.put(ms_api.BASE + '/entity/customerorder/' + row['id'],
                                headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                                         'Content-Type': 'application/json'},
                                json=payload, timeout=(5, 45))
        journal('put_result', {'id': row['id'], 'number': row['number'],
                               'http_status': response.status_code})
    except (requests.Timeout, requests.ConnectionError) as exc:
        actual = fresh_order(row)
        journal('put_uncertain', {'id': row['id'], 'number': row['number'],
                                  'error': type(exc).__name__, 'actual_state': source_state(actual)})
        raise RuntimeError('ms_put_uncertain_stop_and_reconcile') from None
    if response.status_code not in (200, 201):
        raise RuntimeError(f'ms_put_http_{response.status_code}_stop')
    actual = fresh_order(row)
    journal('readback', {'id': row['id'], 'number': row['number'],
                         'state': source_state(actual), 'updated': actual.get('updated')})
    if source_state(actual) != desired:
        raise RuntimeError('ms_readback_mismatch_stop')
    return 'updated'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='one-time MS status changes; default is dry')
    parser.add_argument('--limit', type=int, default=MAX_WRITES, help='maximum confirmed MS changes')
    args = parser.parse_args()
    if args.limit < 1 or args.limit > MAX_WRITES:
        raise ValueError('invalid_write_limit')
    if args.apply:
        hour = datetime.now(config.MS_TZ).hour
        if not (hour >= 23 or hour < 5):
            raise RuntimeError('outside_ms_mass_change_window')
        operational_ready()
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(OUT / 'lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        states = state_ids()
        rows, excluded = candidates(states)
        counts = Counter(excluded)
        counts.update({'candidates': len(rows)})
        if args.apply:
            snapshot_path = OUT / ('prewrite_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
            fd = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'period': [FROM, TO], 'rows': rows,
                           'excluded': dict(excluded)}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        journal('start', {'apply': args.apply, 'counts': counts, 'period': [FROM, TO]})
        preview = preview_market(rows) if not args.apply else {}
        changed, last_write, consecutive_market_errors = 0, 0.0, 0
        for row in rows:
            if args.apply:
                hour = datetime.now(config.MS_TZ).hour
                if not (hour >= 23 or hour < 5):
                    raise RuntimeError('outside_ms_mass_change_window')
                if changed >= args.limit:
                    break
                result = apply_one(row, states)
                consecutive_market_errors = (consecutive_market_errors + 1
                                             if result == 'market_unverified' else 0)
                if consecutive_market_errors >= 10:
                    raise RuntimeError('market_api_consecutive_errors_stop')
                if result == 'updated':
                    changed += 1
                    elapsed = time.monotonic() - last_write
                    if elapsed < MIN_WRITE_INTERVAL:
                        time.sleep(MIN_WRITE_INTERVAL - elapsed)
                    last_write = time.monotonic()
                    if changed == 200:
                        journal('canary_200_pause', {'duration_seconds': 600})
                        time.sleep(600)
                        checked = state_ids()
                        if checked != states:
                            raise RuntimeError('ms_canary_metadata_changed_stop')
                        operational_ready()
                        journal('canary_200_read_check', {'ms_metadata': 'ok'})
            else:
                try:
                    market, detail = preview.get((row['account'], row['number'])) or marketplace_status(row)
                    if market is None:
                        raise ValueError(detail)
                    desired = target(row['state'], market)
                    journal('preview', {**row, 'market': market, 'detail': detail, 'target': desired})
                    result = 'would_update_' + desired if desired else 'unchanged'
                except Exception as exc:
                    journal('preview_error', {**row, 'error': type(exc).__name__,
                                               'reason': str(exc)[:120]})
                    result = 'market_unverified'
            counts[result] += 1
        journal('summary', {'apply': args.apply, 'counts': counts})
        print(json.dumps(dict(counts), ensure_ascii=False))


if __name__ == '__main__':
    main()
