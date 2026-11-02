# поток: inv
"""Split a marketplace kit line across real component units without changing its total."""
from __future__ import annotations

from decimal import Decimal

from ops.fbs_ms_order_payload import Line, line_total


def split_kit_price(card_ids: list[str], kit_quantity: int, kit_unit_kopeks: int,
                    discount_percent: Decimal) -> list[Line]:
    """Equal gross allocation; tiny zero-sum adjustments preserve the net kopek total."""
    if (not card_ids or len(card_ids) > 60 or kit_quantity <= 0 or
            kit_unit_kopeks <= 0 or kit_unit_kopeks * kit_quantity < len(card_ids)):
        raise ValueError('set_price_allocation_invalid')
    gross = kit_unit_kopeks * kit_quantity
    base, remainder = divmod(gross, len(card_ids))
    prices = [base + (i < remainder) for i in range(len(card_ids))]
    target = line_total(Line(card_ids[0], Decimal(kit_quantity), kit_unit_kopeks,
                             discount_percent))

    def net(values: list[int]) -> int:
        return sum(line_total(Line(cid, Decimal(1), price, discount_percent))
                   for cid, price in zip(card_ids, values))

    if net(prices) != target:
        found = None
        baseline = [line_total(Line(cid, Decimal(1), price, discount_percent))
                    for cid, price in zip(card_ids, prices)]
        baseline_total = sum(baseline)
        for left in range(len(prices)):
            for right in range(left + 1, len(prices)):
                for delta in range(-20, 21):
                    if prices[left] + delta <= 0 or prices[right] - delta <= 0:
                        continue
                    trial = prices.copy()
                    trial[left] += delta
                    trial[right] -= delta
                    new_total = (baseline_total - baseline[left] - baseline[right]
                                 + line_total(Line(card_ids[left], Decimal(1), trial[left], discount_percent))
                                 + line_total(Line(card_ids[right], Decimal(1), trial[right], discount_percent)))
                    if new_total == target:
                        found = trial
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            # Only rounding cents are reallocated. Never invent a new total price.
            original = [line_total(Line(cid, Decimal(1), p, discount_percent))
                        for cid, p in zip(card_ids, prices)]
            states: dict[tuple[int, int], list[int]] = {(0, 0): []}
            desired = target - sum(original)
            for cid, price, old in zip(card_ids, prices, original):
                next_states = {}
                for (gross_delta, net_delta), moves in states.items():
                    for delta in range(-10, 11):
                        if price + delta <= 0:
                            continue
                        g = gross_delta + delta
                        n = net_delta + line_total(Line(cid, Decimal(1), price + delta,
                                                         discount_percent)) - old
                        if abs(g) <= 20 and abs(n) <= 20:
                            next_states.setdefault((g, n), moves + [delta])
                states = next_states
            moves = states.get((0, desired))
            if moves is None:
                raise ValueError('set_discount_allocation_unresolved')
            found = [price + delta for price, delta in zip(prices, moves)]
        prices = found
    lines = [Line(cid, Decimal(1), price, discount_percent)
             for cid, price in zip(card_ids, prices)]
    if sum(x.price_kopeks for x in lines) != gross or sum(line_total(x) for x in lines) != target:
        raise ValueError('set_price_reconciliation_failed')
    return lines
