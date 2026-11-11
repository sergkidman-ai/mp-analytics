# поток: inv
"""Convert marketplace bundle counts to physical units without changing money."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import re
from ops.fbs_ms_order_payload import Line, line_total

MARKER = re.compile(r'^(\d{4})[XХ](\d+)([A-Z0-9]*)$', re.I)
# Explicitly confirmed by the operator, including the derivative suffix.
CONFIRMED = {'6452X3MS22I0': 3}


def bundle_multiplier(code, title=''):
    match = MARKER.fullmatch(code or '')
    if not match or int(match[2]) <= 1:
        return 1
    multiplier = int(match[2])
    if multiplier > 1000:
        raise ValueError('bundle_quantity_out_of_budget')
    if not match[3] or code.upper() in CONFIRMED:
        return multiplier
    # A random derivative suffix can start with X5/X99. Confirm suffixed codes
    # against the marketplace card, never against a guessed product name.
    quantity = re.search(r'\b(\d+)\s*шт\b', title or '', re.I)
    if (quantity and int(quantity[1]) == multiplier
            and re.match(r'^(?:Комплект|Бандл)\b', title or '', re.I)):
        return multiplier
    if not title:
        raise ValueError('bundle_marker_requires_card_title')
    return 1


def unit_price(price, multiplier, count, discount):
    original = Decimal(str(price))
    total = line_total(Line('', Decimal(count), original, Decimal(discount)))
    exact = original / multiplier
    for rounding in (ROUND_DOWN, ROUND_UP):
        candidate = exact.quantize(Decimal('0.01'), rounding=rounding)
        if (candidate > 0 and abs(candidate*count*multiplier-original*count) < Decimal('.5')
                and line_total(Line('', Decimal(count*multiplier), candidate, Decimal(discount))) == total):
            return int(candidate) if candidate == candidate.to_integral_value() else candidate
    raise ValueError('bundle_price_cannot_preserve_total')


def normalize_market(market, title_lookup=None):
    result = deepcopy(market)
    for item in result['lines']:
        if item.get('bundle_multiplier'):
            # Already normalized source objects are safe to pass through again.
            continue
        if not MARKER.fullmatch(item['market_code']):
            continue
        title = item.get('market_name') or ''
        if not title and title_lookup:
            title = title_lookup(item['market_code']) or ''
        multiplier = bundle_multiplier(item['market_code'], title)
        if multiplier == 1:
            continue
        count = item['quantity']
        if type(count) is not int or count <= 0:
            raise ValueError('bundle_market_quantity_invalid')
        price = item['price_kopeks']
        item.update(bundle_multiplier=multiplier, bundle_market_quantity=count,
                    bundle_unit_price_kopeks=price,
                    quantity=count*multiplier,
                    price_kopeks=unit_price(price,multiplier,count,item['discount_percent']))
    return result
