# поток: inv
"""Narrow read-only MoySklad checks for FBS order recovery."""
from __future__ import annotations

import re

from core import ms_api


def find_customerorder(external_id: str, get=ms_api.get) -> dict | None:
    """Search all states by full marketplace document number."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', external_id):
        raise ValueError('invalid_external_order_id')
    data = get('/entity/customerorder', {'limit': 100, 'filter': f'name={external_id}'})
    rows = data.get('rows', [])
    if data.get('meta', {}).get('size') != len(rows):
        raise RuntimeError('customerorder_search_incomplete')
    exact = [row for row in rows if row.get('name') == external_id]
    if len(exact) > 1:
        raise RuntimeError('duplicate_customerorder_number')
    return exact[0] if exact else None


def product_cards(external_code: str, get=ms_api.get) -> list[dict]:
    if not external_code or ';' in external_code or len(external_code) > 80:
        raise ValueError('invalid_external_code')
    rows, offset = [], 0
    while True:
        result = get('/entity/assortment', {'limit': 100, 'offset': offset,
                     'filter': [f'externalCode={external_code}', 'archived=false'],
                     'expand': 'supplier'})
        page = result.get('rows', [])
        rows.extend(page)
        size = result.get('meta', {}).get('size')
        if not isinstance(size, int) or (size > len(rows) and not page):
            raise RuntimeError('product_search_incomplete')
        if len(rows) >= size:
            break
        offset += len(page)
        if offset >= 1000:
            raise RuntimeError('product_page_budget')
    return [r for r in rows if r.get('externalCode') == external_code
            and r.get('meta', {}).get('type') == 'product' and not r.get('archived')]


def warehouse_stock(store_id: str, cards: list[dict], get=ms_api.get) -> dict[str, dict]:
    if not cards or len(cards) > 20 or len({c['id'] for c in cards}) != len(cards):
        raise ValueError('stock_card_budget_or_duplicates')
    store = ms_api.ref('store', store_id)['meta']['href']
    found = {c['id']: {'stock': 0, 'reserve': 0} for c in cards}
    wanted = {c['id'] for c in cards}
    offset = 0
    while True:
        result = get('/report/stock/all', {'limit': 100, 'offset': offset,
                     'filter': [f'store={store}', 'stockMode=all'] +
                               [f"product={c['meta']['href']}" for c in cards]})
        page = result.get('rows', [])
        for row in page:
            cid = row.get('meta', {}).get('href', '').split('?', 1)[0].rsplit('/', 1)[-1]
            if cid not in wanted or cid in found and found[cid].get('_seen'):
                raise RuntimeError('stock_response_identity_or_duplicate')
            found[cid] = {'stock': row.get('stock', 0), 'reserve': row.get('reserve', 0), '_seen': True}
        size = result.get('meta', {}).get('size')
        if not isinstance(size, int) or (size > offset + len(page) and not page):
            raise RuntimeError('stock_response_incomplete')
        offset += len(page)
        if offset >= size:
            break
        if offset >= 1000:
            raise RuntimeError('stock_page_budget')
    for value in found.values():
        value.pop('_seen', None)
    return found
