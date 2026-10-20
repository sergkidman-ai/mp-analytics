# поток: fin
"""FBS list v4 / FBO list v3 -> untouched raw_ozon_posting.

Both currently return root postings/has_next/cursor (verified separately).
Fetch all statuses in a fixed inclusive UTC window, without sort assumptions.
No partial-window commits and no unbounded rate-limit retries.
"""
import sys
import time
import pathlib
import math
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from core.ozon_posting import PostingProtocolError, product_price, rub, money_amount  # noqa: E402
from collectors.ozon import _headers  # noqa: E402

FBS_URL = 'https://api-seller.ozon.ru/v4/posting/fbs/list'
FBO_URL = 'https://api-seller.ozon.ru/v3/posting/fbo/list'
MAX_ATTEMPTS = 5
PAGE_BUDGET = 120
WINDOW_BUDGET = 900
MAX_PAGES = 10000


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def request_page(url, headers, body, *, deadline=None):
    """Bound each page by attempts and elapsed time, including server pauses."""
    deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + PAGE_BUDGET)
    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Ozon posting page retry budget exhausted')
        try:
            response = requests.post(url, headers=headers, json=body,
                                     timeout=(min(5, remaining/4), min(30, remaining*3/4)))
        except (requests.Timeout, requests.ConnectionError):
            response = None
        if response is not None and response.status_code != 429 and not 500 <= response.status_code <= 599:
            response.raise_for_status()
            try:
                result = response.json()
            except ValueError:
                raise PostingProtocolError('Ozon posting page is not JSON') from None
            if time.monotonic() > deadline:
                raise TimeoutError('Ozon posting page time budget exhausted')
            return result
        if attempt + 1 == MAX_ATTEMPTS:
            code = response.status_code if response is not None else 'transport'
            raise RuntimeError(f'Ozon posting retries exhausted ({code}, {MAX_ATTEMPTS} attempts)')
        pause = max(2 ** (attempt + 1), retry_after(response.headers.get('Retry-After')) if response is not None else 0)
        if pause >= deadline - time.monotonic():
            raise TimeoutError('Ozon posting Retry-After exceeds remaining budget')
        time.sleep(pause)
    raise AssertionError('unreachable')


def validate_posting(posting, start, end, scheme):
    if not isinstance(posting, dict) or not isinstance(posting.get('posting_number'), str) or not posting['posting_number']:
        raise PostingProtocolError('Posting number missing')
    if not isinstance(posting.get('status'), str) or not posting['status']:
        raise PostingProtocolError('Posting status missing')
    stamp = posting.get('in_process_at') or posting.get('created_at')
    try:
        when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        inside = when.tzinfo is not None and start <= when <= end
    except (AttributeError, TypeError, ValueError):
        inside = False
    if not inside:
        raise PostingProtocolError(f'{scheme} posting outside requested UTC window or missing timestamp')
    products = posting.get('products')
    if not isinstance(products, list) or not products:
        raise PostingProtocolError('Posting products missing')
    for p in products:
        if not isinstance(p, dict) or not p.get('offer_id') or not p.get('sku'):
            raise PostingProtocolError('Posting product identity missing')
        if type(p.get('quantity')) is not int or p['quantity'] <= 0:
            raise PostingProtocolError('Invalid posting quantity')
        if not isinstance(p.get('price'), dict) or product_price(p) < 0:
            raise PostingProtocolError('New posting list product price must be Money')
    fd = posting.get('financial_data')
    # Empty financial data is legitimate before completion/cancellation.
    if fd is not None:
        if not isinstance(fd, dict) or not isinstance(fd.get('products', []), list):
            raise PostingProtocolError('Invalid financial_data')
        for item in fd.get('products', []):
            if not isinstance(item, dict):
                raise PostingProtocolError('Invalid financial product')
            for field in ('price', 'old_price', 'payout', 'customer_price', 'total_discount_value'):
                if item.get(field) is not None:
                    parser = money_amount if field == 'customer_price' else rub
                    parser(item[field], legacy_currency='RUB')
    return when


def _window_bound(value, *, end=False):
    """Legacy dates mean whole UTC days; timestamps must carry a timezone."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            try:
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Expected ISO date or timezone-aware timestamp') from None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Posting timestamp must have an explicit timezone')
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        bound = datetime.max.time().replace(microsecond=999000) if end else datetime.min.time()
        return datetime.combine(value, bound, timezone.utc)
    raise ValueError('Expected date or timezone-aware timestamp')


def fetch_postings(account, scheme, date_from, date_to, *, limit=1000,
                   max_pages=None, timeout=WINDOW_BUDGET, statuses=None, sort_dir=None):
    """Read a complete inclusive UTC window, or raise. Never writes to DB.

    date_from/date_to: ISO dates (legacy whole days), aware datetime objects or
    ISO timestamps with Z/offset. Date callers retain their existing contract.
    limit: page size; max_pages: complete-window cap; timeout: total budget in
    seconds, shared by HTTP attempts, Retry-After and pagination pauses.
    Optional FBS statuses uses filter.statuses; only verified sort_dir='DESC' is
    exposed. Requested filtering/order is validated across all returned pages.
    Defaults retain ALL statuses for the financial collector. Return is a list
    of untouched posting dicts, without cache/checkpoint or financial rebuild.
    """
    if scheme not in ('fbs', 'fbo') or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid scheme or page size')
    max_pages = MAX_PAGES if max_pages is None else max_pages
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError('Invalid posting page cap')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Posting timeout must be finite positive seconds')
    if statuses is not None:
        if not isinstance(statuses, (list, tuple)) or not statuses or any(not isinstance(s, str) or not s.strip() for s in statuses):
            raise ValueError('Posting statuses must be a nonempty list of strings')
        statuses = list(dict.fromkeys(statuses))
    if sort_dir not in (None, 'DESC'):
        raise ValueError('Only verified posting sort_dir=DESC is supported')
    if scheme != 'fbs' and (statuses is not None or sort_dir is not None):
        raise ValueError('Status/sort parameters are verified only for FBS')
    start, end = _window_bound(date_from), _window_bound(date_to, end=True)
    if start > end:
        raise ValueError('Reversed posting window')
    headers = _headers(account)
    url = FBS_URL if scheme == 'fbs' else FBO_URL
    flt = {'since': start.isoformat().replace('+00:00', 'Z'),
           'to': end.isoformat().replace('+00:00', 'Z')}
    if statuses is not None:
        flt['statuses'] = statuses
    body = {'filter': flt, 'limit': limit, 'cursor': '', 'with': {'financial_data': True}}
    if sort_dir is not None:
        body['sort_dir'] = sort_dir
    out, numbers, cursors = [], set(), set()
    deadline = time.monotonic() + timeout
    previous_stamp = None
    for index in range(max_pages):
        if time.monotonic() >= deadline:
            raise TimeoutError('Ozon posting window budget exhausted')
        result = request_page(url, headers, body, deadline=deadline)
        if not isinstance(result, dict) or not isinstance(result.get('postings'), list) or type(result.get('has_next')) is not bool or not isinstance(result.get('cursor'), str):
            raise PostingProtocolError('Expected root postings:list / has_next:bool / cursor:str')
        posts, has_next, cursor = result['postings'], result['has_next'], result['cursor']
        if len(posts) > limit:
            raise PostingProtocolError('Posting page exceeds requested limit')
        for p in posts:
            stamp = validate_posting(p, start, end, scheme)
            if statuses is not None and p['status'] not in statuses:
                raise PostingProtocolError('Ozon posting status filter was not respected')
            if sort_dir is not None:
                if not p.get('in_process_at') or (previous_stamp is not None and stamp > previous_stamp):
                    raise PostingProtocolError('Ozon posting DESC order was not respected')
                previous_stamp = stamp
            if p['posting_number'] in numbers:
                raise PostingProtocolError('Duplicate posting across pages')
            numbers.add(p['posting_number'])
        out.extend(posts)
        if time.monotonic() > deadline:
            raise TimeoutError('Ozon posting window budget exhausted')
        print(f'  [oz {scheme}] +{len(posts)} (всего {len(out)})', flush=True)
        if not has_next:
            return out
        if not posts or not cursor or cursor in cursors or cursor == body['cursor']:
            raise PostingProtocolError('Ozon posting pagination did not advance')
        if index + 1 == max_pages:
            raise PostingProtocolError('Ozon posting page limit exhausted')
        cursors.add(cursor)
        body = {**body, 'cursor': cursor}
        if deadline - time.monotonic() <= 0.3:
            raise TimeoutError('Ozon posting pagination pause exceeds remaining budget')
        time.sleep(0.3)
    raise PostingProtocolError('Ozon posting page limit exhausted')


def raw_records(account, posts, scheme, date_from, date_to):
    return [{'account': account, 'posting_number': p['posting_number'], 'scheme': scheme,
             'status': p['status'], 'in_process_at': p.get('in_process_at') or p.get('created_at'),
             'period_from': date_from, 'period_to': date_to,
             'payload': psycopg2.extras.Json(p)} for p in posts]


def load_raw(account, posts, scheme, date_from, date_to):
    return db.upsert('raw_ozon_posting', raw_records(account, posts, scheme, date_from, date_to),
                     conflict_cols=['account', 'posting_number'])


def main(date_from='2026-06-01', date_to='2026-06-30', account='oz_acc1'):
    print(f'Ozon постинги {account} {date_from}..{date_to}', flush=True)
    records = []
    for scheme in ('fbs', 'fbo'):
        records.extend(raw_records(account, fetch_postings(account, scheme, date_from, date_to),
                                   scheme, date_from, date_to))
    # One transaction only after BOTH schemes have completed successfully.
    total = db.upsert('raw_ozon_posting', records, conflict_cols=['account', 'posting_number'])
    print(f'Итого постингов → raw_ozon_posting: {total}', flush=True)


if __name__ == '__main__':
    a = sys.argv
    main(a[1] if len(a) > 1 else '2026-06-01', a[2] if len(a) > 2 else '2026-06-30',
         a[3] if len(a) > 3 else 'oz_acc1')
