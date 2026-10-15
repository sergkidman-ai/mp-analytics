# поток: mkt
"""Ozon action API migration: Money, cursor pagination and verified outcomes.

No transport/credentials here; callers supply their authenticated request function.
"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING


class ActionProtocolError(RuntimeError):
    pass


def amount(value):
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ActionProtocolError('Invalid or missing Ozon Money') from None
    if not number.is_finite() or number < 0:
        raise ActionProtocolError('Invalid Ozon price')
    return float(number)


def money(value):
    number = Decimal(str(amount(value))).quantize(Decimal('.01'))
    return dict(amount=format(number, '.2f'), currency='RUB')


def payload(response):
    if not isinstance(response, dict):
        raise ActionProtocolError('Ozon response is not an object')
    result = response.get('result', response)
    if not isinstance(result, dict):
        raise ActionProtocolError('Ozon result is not an object')
    return result


def product(row, participant):
    if not isinstance(row, dict) or not row.get('id'):
        raise ActionProtocolError('Missing Ozon product id')
    out = dict(row)
    for key in ('price', 'action_price', 'max_action_price'):
        out[key] = amount(row.get(key))
    out['action_price_limit'] = out['action_price']
    seller = row.get('marketplace_seller_price')
    if seller is not None:
        out['marketplace_seller_price'] = amount(seller)
        if participant:
            if out['marketplace_seller_price'] <= 0:
                raise ActionProtocolError('Participant seller price is not positive')
            out['action_price'] = out['marketplace_seller_price']
    elif participant:
        raise ActionProtocolError('Participant has no marketplace_seller_price')
    return out


def products(request, account, action_id, candidates=False):
    endpoint = '/v2/actions/candidates' if candidates else '/v2/actions/products'
    out, seen_ids, seen_cursors, last = [], set(), set(), None
    for _ in range(10000):
        body = dict(action_id=action_id, limit=100)
        if last is not None:
            body['last_id'] = last
        data = payload(request(account, 'POST', endpoint, body))
        rows = data.get('products')
        if not isinstance(rows, list):
            raise ActionProtocolError('Missing Ozon product list')
        for row in rows:
            item = product(row, not candidates)
            pid = int(item['id'])
            if pid in seen_ids:
                raise ActionProtocolError('Duplicate product in action pagination')
            seen_ids.add(pid);out.append(item)
        total = data.get('total_items', data.get('total'))
        if total is not None and len(out) >= int(total):
            return out
        nxt = data.get('last_id')
        if nxt in (None, '', '0', 0):
            if total is not None and len(out) < int(total):
                raise ActionProtocolError('Action pagination ended before total')
            return out
        nxt = str(nxt)
        if not rows or nxt in seen_cursors:
            raise ActionProtocolError('Action pagination cursor did not advance')
        seen_cursors.add(nxt);last = nxt
    raise ActionProtocolError('Action pagination limit exceeded')


def outcome(response, legacy_active=False):
    data = payload(response)
    active = {int(x) for x in data.get('active_product_ids', [])}
    inactive = {int(x) for x in data.get('deactivated_product_ids', [])}
    # Before switch-over update still returns the old activation response.
    if legacy_active and 'active_product_ids' not in data:
        active.update(int(x) for x in data.get('product_ids', []))
    rejected = {int(x['product_id']): x.get('reason', 'rejected')
                for x in data.get('rejected', []) if x.get('product_id') is not None}
    if active & inactive:
        raise ActionProtocolError('Conflicting active/deactivated results')
    return dict(active=active, inactive=inactive, rejected=rejected,
                warnings=data.get('warnings') or [])


def update(request, account, action_id, rows):
    combined = dict(active=set(), inactive=set(), rejected={}, warnings=[])
    for start in range(0, len(rows), 100):
        chunk = []
        for row in rows[start:start+100]:
            item = dict(product_id=int(row['product_id']), action_price=money(row['action_price']))
            if row.get('stock') is not None:
                item['stock'] = row['stock']
            chunk.append(item)
        result = outcome(request(account, 'POST', '/v1/actions/products/update',
                                 dict(action_id=action_id, products=chunk)), legacy_active=True)
        for key in ('active', 'inactive'):combined[key].update(result[key])
        combined['rejected'].update(result['rejected']);combined['warnings'].extend(result['warnings'])
    return combined


def remove(request, account, action, rows):
    """Use price exclusion for stock/elastic, dedicated removal for promo codes.

    Transition bridge: while update retains old activation semantics, v2 deactivate
    still retains old removal semantics. Try it only for explicitly unremoved rows;
    after switch-over successful price exclusion needs no bridge.
    """
    kind = action.get('action_type')
    if kind in ('PROMO_CODE', 'PROMOCODE', 'PROMO_CODES'):
        pending = rows
        result = dict(active=set(), inactive=set(), rejected={}, warnings=[])
    elif kind in ('ELASTIC_BOOSTING', 'STOCK_DISCOUNT'):
        changes = []
        for row in rows:
            cap = amount(row.get('max_price', row.get('cap')))
            if cap <= 0:
                raise ActionProtocolError('Cannot exclude stock/elastic without known action ceiling')
            ceiling = max(cap, float(row.get('floor_price') or row.get('floor') or 0),
                          float(row.get('action_price') or row.get('now_price') or 0))
            price = int(Decimal(str(ceiling)).to_integral_value(rounding=ROUND_CEILING)) + 1
            changes.append(dict(product_id=row['product_id'], action_price=price))
        result = update(request, account, action['id'], changes)
        pending = [row for row in rows if int(row['product_id']) not in result['inactive']]
    else:
        raise ActionProtocolError(f'Unsupported action type: {kind}')
    for start in range(0, len(pending), 1000):
        ids = [int(row['product_id']) for row in pending[start:start+1000]]
        data = payload(request(account,'POST','/v2/actions/products/deactivate',
                               dict(action_id=action['id'], product_ids=ids)))
        removed = {int(x) for x in data.get('product_ids', [])}
        result['inactive'].update(removed);result['active'].difference_update(removed)
        for pid in removed:result['rejected'].pop(pid,None)
        if kind in ('ELASTIC_BOOSTING','STOCK_DISCOUNT') and removed:
            result['warnings'].append('transition bridge: v2 deactivate used')
    return result
