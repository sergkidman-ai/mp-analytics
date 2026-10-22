#!/usr/bin/env python3
# поток: fin
"""Recoverable WB auto-action price controller. Default: read-only preview.

Membership cannot be read for auto actions: verification means seller price, not
confirmed action membership. Native auto-discount blocking remains a separate LK import.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import gzip
import json
import os
from pathlib import Path
import sys
import time

import requests

# Deployed cost/BOM policy is shared with the existing guard, including today's floor.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv('/opt/mp-analytics/.env')
from core.wb_auto_entry import active_offers, choose, instant, number, protection, sale
from core.wb_auto_journal import Journal
from core.wb_price_writer import ROOT, barrier, writer_lock
from core.profit_floor import POLICY_VERSION, minimum_profit
from ops import wb_promo_guard as guard

UTC = timezone.utc
PRICE = 'https://discounts-prices-api.wildberries.ru'
CALENDAR = guard.CAL_API


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Client:
    def __init__(self, account, budget=360):
        self.account = account
        self.deadline = time.monotonic() + budget
        self.last = 0
        self.headers = guard.head(account)
        self.observed_stocks = {}
        self.goods_calls = 0

    def raw(self, name, value):
        path = ROOT / 'raw' / self.account / (name + '.json.gz')
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(gzip.compress(json.dumps({'captured': datetime.now(UTC).isoformat(), 'response': value},
                                                ensure_ascii=False).encode(), compresslevel=3))
        tmp.replace(path)

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value < 2:
            raise TimeoutError('account run budget exhausted')
        return value

    def request(self, method, url, *, safe=True, headers=None, **kwargs):
        for attempt in range(4 if safe else 1):
            delay = max(0, .65 - (time.monotonic() - self.last))
            if self.remaining() < delay + 2:
                raise TimeoutError('request budget exhausted')
            time.sleep(delay)
            self.last = time.monotonic()
            try:
                response = requests.request(method, url, headers=headers or self.headers,
                                            timeout=min(25, self.remaining()), allow_redirects=False, **kwargs)
            except requests.RequestException:
                if not safe or attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, max(0, self.remaining() - 2)))
                continue
            if safe and (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                retry = response.headers.get('Retry-After') or response.headers.get('X-Ratelimit-Retry') or str(2 ** attempt)
                try:
                    retry = min(120, max(1, float(retry)))
                except ValueError:
                    retry = 3
                if retry + 2 > self.remaining():
                    raise TimeoutError('WB rate limit exceeds run budget')
                time.sleep(retry)
                continue
            return response
        raise RuntimeError('request attempts exhausted')

    def get(self, path, params=None, *, url=None):
        response = self.request('GET', url or PRICE + path, params=params)
        response.raise_for_status()
        value = response.json()
        if value.get('error'):
            raise RuntimeError('WB response error: ' + str(value.get('errorText', ''))[:150])
        return value

    def goods(self, nm_id=None):
        out, invalid, seen = {}, {}, set()
        self.goods_calls += 1
        slot = self.goods_calls % 3
        for page in range(1 if nm_id is not None else 100):
            params = {'limit': 1000, 'offset': page * 1000}
            if nm_id is not None:
                params['filterNmID'] = int(nm_id)
            value = self.get('/api/v2/list/goods/filter', params)
            self.raw(f'prices_{slot}_page_{page:03}', value)
            if not isinstance(value.get('data', {}).get('listGoods'), list):
                raise RuntimeError('invalid goods response')
            rows = value['data']['listGoods']
            for row in rows:
                nm = int(row['nmID'])
                if nm in seen:
                    raise RuntimeError('goods pagination duplicate')
                seen.add(nm)
                sizes = row.get('sizes') or []
                if not sizes or row.get('currencyIsoCode4217') != 'RUB':
                    invalid[nm] = 'missing_size_or_non_RUB'
                    continue
                prices = {(number(s['price']), number(s['discountedPrice'])) for s in sizes}
                if len(prices) != 1:
                    invalid[nm] = 'different_size_prices'
                    continue
                base, current = prices.pop()
                discount = number(row['discount'])
                if base <= 0 or base != int(base) or discount != int(discount) or not 0 <= discount <= 99:
                    invalid[nm] = 'invalid_price'
                    continue
                if abs(sale(base, discount) - current) > Decimal('.01'):
                    invalid[nm] = 'price_discount_mismatch'
                    continue
                out[nm] = (float(base), float(discount), float(current), row.get('vendorCode'))
            if len(rows) < 1000:
                if nm_id is not None and (set(out) != {int(nm_id)}):
                    raise RuntimeError('point price response mismatch')
                self.raw(f'prices_{slot}_complete', {'pages': page + 1, 'valid': len(out), 'invalid': len(invalid), 'nm_id': nm_id})
                return out, invalid
        raise RuntimeError('goods page bound reached')

    def calendar(self):
        now = datetime.now(UTC)
        value = self.get('', {'startDateTime': (now - timedelta(days=65)).isoformat(),
                             'endDateTime': (now + timedelta(days=30)).isoformat(),
                             'allPromo': 'false', 'limit': 100}, url=CALENDAR)
        promos = value.get('data', {}).get('promotions')
        self.raw('calendar', value)
        if not isinstance(promos, list) or len(promos) >= 100:
            raise RuntimeError('calendar incomplete')
        return {int(p['id']): p for p in promos}

    def stocks(self, start=0, initial=None, checkpoint=None):
        """Read-only analytics POST, bounded unlike legacy mp_stock's 429 loop."""
        token = os.environ.get(guard.FUNNEL_TOKEN_ENV[self.account])
        if not token:
            return {}
        end = datetime.now(UTC).astimezone(guard.MSK).date()
        result = dict(initial or {})
        self.observed_stocks = result
        last = 0
        for page in range(60):
            delay = max(0, 21 - (time.monotonic() - last))
            if self.remaining() < delay + 5:
                raise TimeoutError('stock warm budget exhausted; progress checkpointed')
            time.sleep(delay)
            body = {'nmIDs': [], 'brandNames': [], 'subjectIDs': [], 'tagIDs': [],
                    'selectedPeriod': {'start': (end - timedelta(days=30)).isoformat(), 'end': end.isoformat()},
                    'orderBy': {'field': 'openCard', 'mode': 'desc'}, 'limit': 1000, 'offset': start + page * 1000}
            last = time.monotonic()
            response = self.request('POST', guard.FUNNEL_URL, json=body,
                                    headers={'Authorization': token, 'Content-Type': 'application/json'})
            response.raise_for_status()
            rows = response.json().get('data', {}).get('products')
            if not isinstance(rows, list):
                raise RuntimeError('invalid analytics stock response')
            for row in rows:
                product = row['product']
                nm = int(product['nmId'])
                stock = product.get('stocks') or {}
                qty = int(stock.get('mp') or 0) + int(stock.get('wb') or 0)
                # Sales-ranked pagination can move during the run. Repeated rows do
                # not prove zero stock for absent rows; retain the conservative value.
                result[nm] = min(result.get(nm, qty), qty)
            if checkpoint:
                checkpoint(result, start + (page + 1) * 1000, len(rows) < 1000)
            if len(rows) < 1000:
                return result
        raise RuntimeError('stock page bound reached')


def proposals(account, manifest, goods, invalid, cogs, stocks, calendar, now, optimize_current=False):
    rows = []
    keep = Decimal(1) - number(guard.RETENTION[account])
    starts = [instant(o['start']) for offers in manifest['offers'][account].values()
              for o in offers if 3038 in o['ids'] or 3040 in o['ids']]
    optimize = optimize_current or not starts or now >= min(starts)
    for raw_nm, offers in manifest['offers'][account].items():
        nm = int(raw_nm)
        row = {'account': account, 'nm_id': nm, 'reason': invalid.get(nm, 'missing_live_price')}
        rows.append(row)
        if nm not in goods:
            continue
        base, discount, current, vendor = goods[nm]
        row.update(base=base, before_discount=discount, before=current, vendor=vendor)
        cost = cogs.get(nm) or {}
        if not cost.get('cogs') or cost.get('branch') in ('none', 'unmapped', 'no_mp_stock'):
            row['reason'] = 'no_complete_cost_or_supply'
            row['stock'] = stocks.get(nm)
            valid = active_offers(offers, calendar, now)
            if optimize and valid:
                cap = max(number(o['cap']) for o in valid)
                # Unknown analytics presence is not proof of zero stock. Without
                # full COGS do not admit any card on a guessed floor: raise only,
                # above every known active cap, even if stock is zero/unobserved.
                if number(current) <= cap:
                    target = cap.to_integral_value(rounding=ROUND_CEILING) + 1
                    new_base, new_discount = protection(base, discount, target)
                    row.update(reason='exclude_unknown_cost', base=new_base, discount=new_discount,
                               target=str(sale(new_base, new_discount)), cap=str(cap), action=True)
            continue
        units = max(1, int(cost.get('bundle', 1)))
        amount = number(cost['cogs'])
        floor = (amount + minimum_profit(amount, units)) / keep
        row.update(cost=str(amount), units=units, floor=str(floor), cost_source=cost.get('source'),
                   stock=stocks.get(nm), stock_branch=cost.get('branch'), is_set=bool(cost.get('is_set')))
        decision = choose(base, floor, offers, calendar, now)
        row.update(decision)
        if not optimize:
            row['reason'] = 'waiting_new_action_start'
        elif stocks.get(nm) == 0:
            row['reason'] = 'no_mp_stock'
        # Enforce the floor for the authorized candidate scope, including below-floor
        # baseline prices. Protective raising is separate from entering at a lower price.
        if number(current) < floor:
            new_base, new_discount = protection(base, discount, floor)
            if (not optimize or stocks.get(nm) == 0 or decision['reason'] != 'eligible'
                    or number(decision['target']) < floor):
                row.update(reason='protect_floor', base=new_base, discount=new_discount,
                           target=str(sale(new_base, new_discount)))
        if row['reason'] in ('eligible', 'protect_floor'):
            target = number(row['target'])
            if target < number(current) and (stocks.get(nm, 0) <= 0):
                row['reason'] = 'stock_not_confirmed_for_price_decrease'
            elif int(row['base']) == int(base) and int(row['discount']) == int(discount) and target == number(current):
                row['reason'] = 'already_at_target'
            else:
                row['action'] = True
                row['net_target'] = str(target * keep - amount)
    return rows


def exact(row, goods):
    got = goods.get(row['nm_id'])
    return bool(got and got[3] == row['vendor'] and number(got[0]) == number(row['base'])
                and int(got[1]) == row['discount'] and number(got[2]) == number(row['target']))


def recover(client, journal, account, goods):
    pending = journal.pending(account)
    if pending:
        barrier(account).touch()
    for task in pending:
        rows = json.loads(task['payload'])
        if not task['upload_id']:
            # Crash after committing intent can be before OR after POST. No exactly-once
            # primitive exists: never guess and send a second asynchronous task.
            journal.update(task['id'], 'uncertain', error='No uploadID; manual reconciliation required',
                           detail={'matched': sum(exact(row, goods) for row in rows), 'n': len(rows)})
            continue
        value = client.get('/api/v2/history/tasks', {'uploadID': task['upload_id']}).get('data') or {}
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else {}
        status = value.get('status')
        # WB also returns terminal status 6 for processed uploads where all items
        # were rejected (e.g. price quarantine / excessive decrease). It is not queued.
        if status not in (3, 4, 5, 6):
            journal.update(task['id'], 'pending', detail={'task_status': status})
            continue
        # Whole-task success counters never substitute for item-level actual prices.
        matches = [row['nm_id'] for row in rows if exact(row, goods)]
        missing = [row['nm_id'] for row in rows if not exact(row, goods)]
        details = client.get('/api/v2/history/goods/task',
                             {'uploadID': task['upload_id'], 'limit': 1000, 'offset': 0})
        journal.update(task['id'], 'verified' if not missing else 'partial', terminal=True,
                       detail={'matched': matches, 'unconfirmed': missing, 'task': value, 'goods': details,
                               'observed': {str(row['nm_id']): goods.get(row['nm_id']) for row in rows}})
    pending = journal.pending(account)
    if not pending:
        barrier(account).unlink(missing_ok=True)
    return not pending


def submit(client, journal, account, rows):
    # Caller owns the writer lock. Barrier is durable before the irreversible network call.
    atomic_json(barrier(account), {'created': time.time(), 'account': account})
    uid = journal.intent(account, rows)
    body = {'data': [{'nmID': r['nm_id'], 'price': int(r['base']), 'discount': int(r['discount'])} for r in rows]}
    try:
        response = client.request('POST', PRICE + '/api/v2/upload/task', safe=False, json=body)
        if response.status_code == 429:
            journal.update(uid, 'rate_limited', terminal=True, error='HTTP 429; retry next bounded run')
            return False
        if response.status_code >= 500 or response.status_code in (408, 409, 425):
            journal.update(uid, 'uncertain', error=f'HTTP {response.status_code}; no blind POST retry')
            return False
        if response.status_code >= 400:
            journal.update(uid, 'rejected', terminal=True, error=f'HTTP {response.status_code}')
            return False
        data = response.json()
        upload_id = (data.get('data') or {}).get('id')
        if not upload_id:
            journal.update(uid, 'uncertain', error='Accepted response without uploadID')
            return False
        journal.update(uid, 'accepted', upload_id=int(upload_id), detail={'response': data})
        return True
    except (requests.RequestException, ValueError, TimeoutError):
        journal.update(uid, 'uncertain', error='Transport/response failure after durable intent; reconcile before retry')
        return False


def run_account(args, manifest, account):
    client = Client(account, args.budget)
    goods, invalid = client.goods()
    if len(goods) < 5000:
        raise RuntimeError('live price catalog incomplete; refusing all writes')
    journal = Journal(ROOT / 'journal.sqlite3') if args.apply or args.recover_only is True else None
    if journal and not recover(client, journal, account, goods):
        return {'account': account, 'blocked': 'unresolved_upload', 'pending': len(journal.pending(account))}
    if args.recover_only is True:
        return {'account': account, 'recovered': True, 'pending_batches': 0, 'price_posts': 0}
    calendar_warning = None
    original_deadline = client.deadline
    client.deadline = min(original_deadline, time.monotonic() + 20)
    try:
        calendar = client.calendar()
    except (requests.RequestException, RuntimeError, TimeoutError) as exc:
        calendar = {}
        calendar_warning = type(exc).__name__ + ': ' + str(exc)[:120]
    finally:
        client.deadline = original_deadline
    stocks_path = ROOT / (account + '_stocks.json')
    stocks = {}
    stock_warning = None
    if stocks_path.exists():
        cached = json.loads(stocks_path.read_text())
        if time.time() - cached['captured'] < 1800:
            stocks = {int(k): v for k, v in cached['stocks'].items()}
    if not stocks:
        # Analytics is rate-limited independently. A separate read-only warmer keeps
        # snapshots fresh; never delay an urgent floor raise by several minutes.
        stock_warning = 'No fresh analytics cache; price decreases deferred, raises allowed'
    cogs = guard.cogs_map(account, goods)
    if sum(bool(cogs.get(nm, {}).get('cogs')) for nm in goods) < guard.COGS_MIN_KNOWN:
        raise RuntimeError('cost dictionary incomplete; refusing price changes')
    now = instant(args.at) if args.at else datetime.now(UTC)
    if args.apply and args.at:
        raise RuntimeError('simulated time forbidden in apply')
    rows = proposals(account, manifest, goods, invalid, cogs, stocks, calendar, now,
                     optimize_current=args.pilot_active is True)
    candidates = [r for r in rows if r.get('action')]
    if args.pilot_active is True:
        # Validate the discount-only write path on an already active action, using
        # actual time. Tiny price-increase-only pilot cannot lower a buyer price.
        candidates = [r for r in candidates if r['reason'] == 'eligible'
                      and r['before_discount'] > 0 and stocks.get(r['nm_id'], 0) > 0
                      and number(r['base']) == number(goods[r['nm_id']][0])
                      and number(r['before']) < number(r['target']) <= number(r['before']) * Decimal('1.15')]
    # Prioritize emergency floor raises before optimizing promotion participation.
    def priority(row):
        if 'floor' in row and number(row['before']) < number(row['floor']):
            return 0
        return 1 if row['reason'] == 'exclude_unknown_cost' else 2
    candidates.sort(key=lambda r: (priority(r), r['nm_id']))
    if args.apply:
        failures = journal.recent_failures(account)
        candidates = [r for r in candidates if priority(r) < 2
                      or failures.get(r['nm_id'], 0) < 3]
    selected = candidates[:args.limit] if args.limit else candidates
    atomic_json(Path(args.out) / (account + '_plan.json'),
                {'captured': now, 'policy': POLICY_VERSION, 'manifest': manifest['sha256'],
                 'mode': 'active_price_raise_pilot' if args.pilot_active is True else 'simulation' if args.at else 'controller',
                 'rows': rows})
    summary = {'account': account, 'goods': len(goods), 'stocks': len(stocks),
               'reasons': dict(Counter(r['reason'] for r in rows)), 'selected': len(selected),
               'accepted_batches': 0, 'submitted_products': 0, 'apply': args.apply}
    summary['below_floor_before'] = sum(number(r['before']) < number(r['floor']) for r in rows if 'floor' in r)
    if stock_warning:
        summary['stock_warning'] = stock_warning
    if calendar_warning:
        summary['calendar_warning'] = calendar_warning
    if not args.apply or not selected:
        summary['below_floor_after'] = summary['below_floor_before']
        return summary
    # Fresh full snapshot after the potentially expensive cost/stock calculation. Preserve
    # externally edited bases/vendors/discounts; never overwrite based on yesterday's XLSX.
    fresh, _ = client.goods()
    selected = [r for r in selected if r['nm_id'] in fresh and fresh[r['nm_id']] == goods[r['nm_id']]]
    summary['cas_skipped'] = summary['selected'] - len(selected)
    if args.pilot_active is not True:
        # WB can apply an auto discount *during* our read/compute phase. A changed
        # price below floor needs a fresh protective quote, not merely a CAS skip.
        replanned = proposals(account, manifest, fresh, {}, cogs, stocks, calendar, datetime.now(UTC))
        urgent = [r for r in replanned if r.get('action') and 'floor' in r
                  and number(r['before']) < number(r['floor'])
                  and r['nm_id'] in goods and fresh[r['nm_id']][3] == goods[r['nm_id']][3]]
        merged = {r['nm_id']: r for r in selected}
        merged.update({r['nm_id']: r for r in urgent})
        selected = sorted(merged.values(), key=lambda r: (priority(r), r['nm_id']))
        if args.limit:
            selected = selected[:args.limit]
        summary['urgent_replanned'] = len(urgent)
    summary['selected_after_cas'] = len(selected)
    snapshot_at = time.monotonic()
    for start in range(0, len(selected), 100):
        if client.remaining() < 30 or time.monotonic() - snapshot_at > 120:
            summary['deferred'] = len(selected) - start
            break
        batch = selected[start:start + 100]
        if not submit(client, journal, account, batch):
            summary['write_error'] = 'Upload rejected or uncertain; inspect durable journal'
            break
        summary['accepted_batches'] += 1
        summary['submitted_products'] += len(batch)
    # Actual confirmation uses another live price snapshot. Pending tasks survive restarts.
    if summary['accepted_batches'] and client.remaining() > 45:
        time.sleep(min(10, client.remaining() - 30))
        after, _ = client.goods()
        recover(client, journal, account, after)
        summary['verified_products'] = sum(exact(row, after) for row in selected)
        summary['below_floor_after'] = sum(number(after[r['nm_id']][2]) < number(r['floor'])
                                           for r in rows if 'floor' in r and r['nm_id'] in after)
    if not journal.pending(account):
        barrier(account).unlink(missing_ok=True)
    summary['pending_batches'] = len(journal.pending(account))
    if not summary['pending_batches'] and summary.get('verified_products', 0) < summary['submitted_products']:
        summary['write_error'] = 'Some accepted products are not confirmed at the target price'
    return summary


def warm_stocks(args):
    results = []
    try:
        with writer_lock('stock_warmer'):
            for account in args.account or list(guard.TOKEN_ENV):
                path = ROOT / (account + '_stocks.json')
                build_path = ROOT / (account + '_stocks_build.json')
                published = json.loads(path.read_text()) if path.exists() else {}
                published_age = time.time() - published.get('captured', 0)
                # Refresh before TTL expiry, without replacing a still-valid complete
                # snapshot with the first 1000 rows of the next bounded scan.
                if published.get('complete') and published_age < 1200:
                    results.append({'account': account, 'stock_cache': 'fresh', 'n': len(published['stocks'])})
                    continue
                cached = json.loads(build_path.read_text()) if build_path.exists() else {}
                if not cached and published and not published.get('complete') and published_age < 1800:
                    cached = dict(published)  # migrate/resume a cold-start partial scan
                if not cached or cached.get('complete') or time.time() - cached.get('captured', 0) >= 1800:
                    cached = {'captured': time.time(), 'stocks': {}, 'next_offset': 0, 'complete': False}
                def checkpoint(stocks, offset, complete):
                    cached.update(stocks=stocks, next_offset=offset, complete=complete)
                    atomic_json(build_path, cached)
                    if complete or not published.get('complete') or time.time() - published.get('captured', 0) >= 1800:
                        atomic_json(path, cached)
                client = Client(account, args.budget)
                warning = None
                failed = False
                try:
                    client.stocks(cached.get('next_offset', 0), {int(k): v for k, v in cached['stocks'].items()}, checkpoint)
                except (requests.RequestException, RuntimeError, TimeoutError) as exc:
                    warning = type(exc).__name__ + ': ' + str(exc)[:160]
                    failed = isinstance(exc, RuntimeError) or (
                        isinstance(exc, requests.HTTPError) and exc.response is not None
                        and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429)
                result = {'account': account, 'n': len(cached['stocks']), 'complete': cached.get('complete'),
                          'next_offset': cached.get('next_offset'), 'warning': warning, 'error': failed}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            atomic_json(ROOT / 'stock_warmer_status.json', {'captured': datetime.now(UTC), 'results': results})
    except BlockingIOError:
        return 75
    return 1 if any(r.get('error') for r in results) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default=str(ROOT / 'manifest.json'))
    parser.add_argument('--out', default=str(ROOT / 'latest'))
    parser.add_argument('--account', choices=list(guard.TOKEN_ENV), action='append')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--budget', type=int, default=360, help='seconds per account, all retries bounded')
    parser.add_argument('--at', help='read-only simulation UTC time')
    parser.add_argument('--warm-stocks', action='store_true', help='read-only resumable analytics stock cache')
    parser.add_argument('--pilot-active', action='store_true', help='max 3 price raises/account in currently active actions')
    parser.add_argument('--recover-only', action='store_true', help='read WB task/price state and reconcile local journal, no price POST')
    args = parser.parse_args()
    if args.limit < 0 or args.budget < 60 or (args.apply and args.at):
        parser.error('invalid limits or apply with simulated time')
    if args.pilot_active and (not 1 <= args.limit <= 3 or args.at or args.warm_stocks):
        parser.error('--pilot-active requires --limit 1..3 and actual time')
    if args.recover_only and (args.apply or args.at or args.pilot_active or args.warm_stocks):
        parser.error('--recover-only cannot be combined with price writes/simulation/warming')
    if args.warm_stocks:
        if args.apply:
            parser.error('--warm-stocks cannot write prices')
        return warm_stocks(args)
    manifest = json.loads(Path(args.manifest).read_text())
    expected = manifest.pop('sha256')
    if hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != expected:
        raise RuntimeError('manifest hash mismatch')
    manifest['sha256'] = expected
    if datetime.now(UTC) > max(instant(o['end']) for offers in manifest['offers'].values()
                               for products in offers.values() for o in products):
        if list(ROOT.glob('*.pending')) or args.recover_only:
            args.recover_only = True
        else:
            print('All configured auto actions ended; no API calls', flush=True)
            return 0
    ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    started = datetime.now(UTC)
    try:
        with writer_lock():
            atomic_json(Path(args.out) / 'running.json', {'pid': os.getpid(), 'started': started})
            accounts = args.account or list(guard.TOKEN_ENV)
            if args.apply:
                Journal(ROOT / 'journal.sqlite3')  # schema before worker connections
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(run_account, args, manifest, account): account for account in accounts}
                for future in as_completed(futures):
                    account = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {'account': account, 'error': type(exc).__name__ + ': ' + str(exc)[:200]}
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
            atomic_json(Path(args.out) / 'status.json', {'captured': datetime.now(UTC), 'results': results})
            atomic_json(Path(args.out) / 'running.json', {'pid': os.getpid(), 'started': started, 'finished': datetime.now(UTC)})
    except BlockingIOError:
        print('Another WB price writer is running; deferred', flush=True)
        return 75
    return 1 if any(r.get('error') or r.get('blocked') or r.get('write_error') or r.get('calendar_warning')
                    or r.get('pending_batches') or r.get('deferred') for r in results) else 0


def legacy_push(account, batch):
    """Durable writer used by old guard/plan_raise, with fresh-price safety checks."""
    try:
        with writer_lock():
            client = Client(account, 120)
            journal = Journal(ROOT / 'journal.sqlite3')
            goods, _ = client.goods()
            if not recover(client, journal, account, goods):
                return False, 'Prior WB upload unresolved; deferred without resending'
            costs = guard.cogs_map(account, goods)
            keep = Decimal(1) - number(guard.RETENTION[account])
            rows, satisfied = [], []
            manifest_path = ROOT / 'manifest.json'
            managed = None
            if any(r.get('wave') == 'plan_raise' for r in batch) and manifest_path.exists():
                configured = json.loads(manifest_path.read_text())
                if datetime.now(UTC) <= max(instant(o['end']) for offers in configured['offers'][account].values() for o in offers):
                    managed = configured['offers'][account]
            for old in batch:
                nm = int(old['nm_id'])
                if old.get('wave') == 'plan_raise' and managed is not None and str(nm) in managed:
                    return False, 'Card managed by wb_auto_entry MAX(active cap); legacy MIN(cap) deferred'
                got = goods.get(nm)
                if not got:
                    return False, 'Missing live price; no upload sent'
                base = int(old.get('send_price') or number(old['target_price']).to_integral_value(rounding=ROUND_CEILING))
                discount = int(old.get('send_disc') or 0)
                target = sale(base, discount)
                cost = costs.get(nm) or {}
                amount, floor = None, None
                units = max(1, int(cost.get('bundle', 1)))
                if cost.get('cogs'):
                    amount = number(cost['cogs'])
                    floor = (amount + minimum_profit(amount, units)) / keep
                    if target < floor:
                        base, discount = protection(got[0], got[1], floor)
                        target = sale(base, discount)
                if number(got[2]) >= target:
                    satisfied.append(nm)
                    continue
                if old.get('price_before') is not None and number(old['price_before']) != number(got[0]):
                    return False, 'Base changed since legacy calculation; deferred'
                rows.append({'nm_id': nm, 'base': base, 'discount': discount,
                             'target': str(target), 'vendor': got[3], 'reason': 'legacy_guard_raise',
                             'cost': str(amount) if amount is not None else None,
                             'floor': str(floor) if floor is not None else None,
                             'units': units, 'cost_source': cost.get('source')})
            if not rows:
                return True, ''
            if not submit(client, journal, account, rows):
                if not journal.pending(account):
                    barrier(account).unlink(missing_ok=True)
                return False, 'WB upload not accepted; durable journal retained'
            while client.remaining() > 35:
                time.sleep(5)
                after, _ = client.goods()
                settled = recover(client, journal, account, after)
                if settled:
                    ok = all(exact(row, after) for row in rows)
                    return ok, '' if ok else 'Partial WB application; inspect durable item journal'
            return False, 'WB task pending; saved for next controller run, no blind retry'
    except BlockingIOError:
        return False, 'Another WB writer active; deferred'
    except Exception as exc:
        return False, type(exc).__name__ + ': ' + str(exc)[:120]


def quarantine_push(account, nm, price):
    """Single-card quarantine write: fresh complete cost, common floor, durable intent.

    A quarantine ladder may decrease prices, but never below the current profit floor.
    Existing Ozon/Market quarantine behavior is outside this adapter.
    """
    try:
        with writer_lock():
            client = Client(account, 75)
            journal = Journal(ROOT / 'journal.sqlite3')
            if journal.pending(account):
                return False, 'WB upload pending; quarantine deferred'
            goods, _ = client.goods(nm)
            got = goods[int(nm)]
            cost = guard.cogs_map(account, goods).get(int(nm)) or {}
            if not cost.get('cogs'):
                return False, 'No complete current COGS; quarantine price not sent'
            amount = number(cost['cogs'])
            keep = Decimal(1) - number(guard.RETENTION[account])
            floor = (amount + minimum_profit(amount, max(1, int(cost.get('bundle', 1))))) / keep
            target = number(price)
            if target != int(target) or target < floor:
                return False, 'Quarantine target below current profit floor; no upload sent'
            row = {'nm_id': int(nm), 'base': int(target), 'discount': 0, 'target': str(sale(target, 0)),
                   'vendor': got[3], 'floor': str(floor), 'cost': str(amount), 'reason': 'quarantine_release',
                   'units': max(1, int(cost.get('bundle', 1))), 'cost_source': cost.get('source')}
            if exact(row, goods):
                return True, ''
            if not submit(client, journal, account, [row]):
                if not journal.pending(account):
                    barrier(account).unlink(missing_ok=True)
                return False, 'Quarantine upload not accepted; journal retained'
            while client.remaining() > 10:
                time.sleep(2)
                after, _ = client.goods(nm)
                if recover(client, journal, account, after):
                    ok = exact(row, after)
                    return ok, '' if ok else 'Quarantine price not confirmed by live API'
            return False, 'Quarantine task pending; next controller resumes verification'
    except BlockingIOError:
        return False, 'WB writer busy; quarantine deferred'
    except Exception as exc:
        return False, type(exc).__name__ + ': ' + str(exc)[:120]


if __name__ == '__main__':
    sys.exit(main())
