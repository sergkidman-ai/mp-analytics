# поток: fin
"""WB auto-action price selection. Pure arithmetic; no marketplace writes here."""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP


def number(value):
    value = Decimal(str(value).replace('\u00a0', '').replace('\u202f', '').replace(' ', '').replace(',', '.').replace('%', ''))
    if not value.is_finite():
        raise ValueError('non-finite price')
    return value


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def sale(base, discount):
    return (number(base) * (100 - int(discount)) / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def active_offers(offers, calendar, now):
    """Exact generation/window match; grouped IDs do not prove individual membership."""
    valid = []
    for offer in offers:
        for promo_id in offer['ids']:
            promo = calendar.get(int(promo_id))
            if (promo and promo['type'] == 'auto'
                    and promo['startDateTime'] == offer['start']
                    and promo['endDateTime'] == offer['end']
                    and instant(offer['start']) <= now <= instant(offer['end'])):
                valid.append(offer)
                break
    return valid


def choose(base, floor, offers, calendar, now):
    """Highest eligible active cap, excluding future/expired or changed generations."""
    base, floor = number(base), number(floor)
    if base <= 0 or base != int(base) or floor <= 0:
        return {'reason': 'invalid_base_or_floor'}
    valid = active_offers(offers, calendar, now)
    if not valid:
        return {'reason': 'no_active_verified_offer'}
    selected = max(valid, key=lambda o: number(o['cap']))
    cap = number(selected['cap'])
    if cap < floor:
        return {'reason': 'cap_below_floor', 'cap': str(cap), 'offer': selected}
    discount = max(0, int((100 * (1 - cap / base)).to_integral_value(rounding=ROUND_CEILING)))
    # WB integer discount is 0..99. Verify rounded price, never tolerate a floor breach.
    if discount > 99:
        return {'reason': 'discount_out_of_range', 'cap': str(cap)}
    target = sale(base, discount)
    if target > cap or target < floor:
        return {'reason': 'no_integer_discount', 'cap': str(cap), 'offer': selected}
    return {'reason': 'eligible', 'base': int(base), 'discount': discount,
            'target': str(target), 'cap': str(cap), 'floor': str(floor), 'offer': selected}


def protection(base, discount, floor):
    """Existing guard behavior: raise only, including base if discount=0 is insufficient."""
    base, floor = number(base), number(floor)
    target_base = max(int(base), int(floor.to_integral_value(rounding=ROUND_CEILING)))
    d = max(0, min(99, int(100 * (1 - floor / target_base))))
    if sale(target_base, d) < floor:
        d = max(0, d - 1)
    return target_base, d
