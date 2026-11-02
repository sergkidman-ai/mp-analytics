# поток: inv
"""Pure selection rules for FBS customer-order recovery (no API writes).

Inputs are verified MS product cards and current Enter purchase prices in kopeks.
Missing or ambiguous evidence is an error, never a reason to guess a product.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from invoice_bot.supplier_groups import ID2GROUP


SPECIAL = {
    '61d01266-2fe1-11f0-0a80-0f7f000bf967': 'wb',
    'bdd618ae-308f-11f0-0a80-153200050962': 'oem',
    'daffd8b7-308f-11f0-0a80-05570004fd14': 't2',
}
SECOND = {'Одиссей', 'Суперфайн', 'Изипринт', 'Спринт', 'Спринт МСК',
          'Сакура', 'Кактус', 'Булат'}
THIRD = {'Блоссом', 'Колортек'}


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Card:
    id: str
    external_code: str
    supplier_id: str
    brand: str
    purchase_kopeks: int | None
    stock: Decimal = Decimal(0)
    reserve: Decimal = Decimal(0)
    archived: bool = False

    @property
    def available(self) -> Decimal:
        return self.stock - self.reserve


def tier(card: Card) -> int | None:
    special_brand = SPECIAL.get(card.supplier_id)
    if special_brand and card.brand.casefold() == special_brand:
        return 1
    group = ID2GROUP.get(card.supplier_id)
    if group in SECOND:
        return 2
    if group in THIRD:
        return 3
    return None


def _verified(cards: list[Card], external_code: str) -> list[Card]:
    if not external_code:
        raise SelectionError('empty_external_code')
    result = [c for c in cards if c.external_code == external_code and not c.archived]
    if not result:
        raise SelectionError('exact_product_card_missing')
    if len({c.id for c in result}) != len(result):
        raise SelectionError('duplicate_card_id')
    return result


def choose_card(cards: list[Card], external_code: str, quantity: Decimal) -> tuple[Card, bool]:
    """Choose an in-stock variant, otherwise lowest current price in earliest tier.

    Returns (card, fully_available). A caller must reserve the selected stock in
    its local allocation pool before considering another order.
    """
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise SelectionError('invalid_quantity')
    exact = _verified(cards, external_code)
    in_stock = [c for c in exact if c.available >= quantity and tier(c) is not None]
    if in_stock:
        # Existing fulfillment planner prefers greatest availability; deterministic tie.
        return sorted(in_stock, key=lambda c: (-c.available, tier(c), c.id))[0], True
    priced = [c for c in exact if tier(c) is not None
              and isinstance(c.purchase_kopeks, int) and c.purchase_kopeks > 0]
    if not priced:
        raise SelectionError('current_purchase_price_missing')
    return min(priced, key=lambda c: (tier(c), c.purchase_kopeks, c.id)), False


class AllocationPool:
    """Track locally promised units until MS stock/reserves can be reread.

    The caller must create one pool per warehouse and discard it on any failed
    or uncertain POST. A new customerorder may not immediately create a reserve,
    so rereading MS alone cannot protect the next import in the same batch.
    """

    def __init__(self):
        self.promised: dict[str, Decimal] = defaultdict(Decimal)

    def choose(self, cards: list[Card], external_code: str,
               quantity: Decimal) -> tuple[Card, bool]:
        from dataclasses import replace

        adjusted = [replace(c, reserve=c.reserve + self.promised[c.id]) for c in cards]
        chosen, available = choose_card(adjusted, external_code, quantity)
        original = next(c for c in cards if c.id == chosen.id)
        if available:
            self.promised[chosen.id] += Decimal(quantity)
        return original, available


def choose_bundle(components: list[tuple[str, Decimal]], cards: list[Card]) -> list[tuple[Card, Decimal]]:
    """Choose exact components, preferably one supplier-brand pair, at most two.

    This function requires full stock for every component. If no such composition
    exists, the caller must keep the parent item for manual review.
    """
    if not components or any(Decimal(q) <= 0 for _, q in components):
        raise SelectionError('invalid_bundle')
    pool = defaultdict(Decimal)
    for code, qty in components:
        pool[code] += Decimal(qty)
    options = {}
    for code, qty in pool.items():
        options[code] = [c for c in _verified(cards, code)
                         if c.available >= qty and tier(c) is not None]
        if not options[code]:
            raise SelectionError('bundle_component_unavailable')
    pairs = { (c.supplier_id, c.brand) for rows in options.values() for c in rows }
    for pair in sorted(pairs):
        if all(any((c.supplier_id, c.brand) == pair for c in options[code]) for code in pool):
            return [(max((c for c in options[code] if (c.supplier_id, c.brand) == pair),
                         key=lambda c: (c.available, -tier(c), c.id)), qty)
                    for code, qty in pool.items()]
    # Small bounded search: sets are normally short. Reject if there is no <=2-pair plan.
    best = None
    codes = list(pool)

    def visit(index, chosen, used_pairs):
        nonlocal best
        if index == len(codes):
            suppliers = {c.supplier_id for c in chosen}
            brands = {c.brand for c in chosen}
            if len(suppliers) <= 2 and len(brands) <= 2:
                score = (len(used_pairs), tuple((tier(c), c.purchase_kopeks or 10**15, c.id) for c in chosen))
                if best is None or score < best[0]:
                    best = (score, tuple(chosen))
            return
        for c in sorted(options[codes[index]], key=lambda x: (tier(x), x.purchase_kopeks or 10**15, x.id)):
            pair = (c.supplier_id, c.brand)
            if len(used_pairs | {pair}) <= 2:
                visit(index + 1, chosen + [c], used_pairs | {pair})

    visit(0, [], set())
    if best is None:
        raise SelectionError('bundle_exceeds_two_supplier_brand_pairs')
    return list(zip(best[1], (pool[code] for code in codes)))


def choose_bundle_plan(components: list[tuple[str, Decimal]], own: list[Card],
                       remote: list[Card]) -> list[tuple[Card, Decimal, bool]]:
    """Complete kit plan: own stock first, then priced remote stock, at most two pairs.

    The boolean says the chosen units are physically on the order warehouse.
    A repeated component may use several cards or both warehouses when needed.
    """
    needs: dict[str, Decimal] = defaultdict(Decimal)
    for code, quantity in components:
        if not code or Decimal(quantity) <= 0:
            raise SelectionError('invalid_bundle')
        needs[code] += Decimal(quantity)
    if not needs:
        raise SelectionError('invalid_bundle')
    options: dict[str, list[tuple[Card, bool]]] = {}
    for code, quantity in needs.items():
        if quantity != quantity.to_integral_value():
            raise SelectionError('noninteger_bundle_component_quantity')
        own_exact = [c for c in own if c.external_code == code and not c.archived]
        remote_exact = [c for c in remote if c.external_code == code and not c.archived]
        if not own_exact and not remote_exact:
            raise SelectionError('exact_product_card_missing:' + code)
        if len({c.id for c in own_exact}) != len(own_exact) or len({c.id for c in remote_exact}) != len(remote_exact):
            raise SelectionError('duplicate_card_id')
        choices = [(card, True) for card in own_exact
                   if card.available >= 1 and tier(card) is not None]
        choices += [(card, False) for card in remote_exact
                    if card.available >= 1 and tier(card) is not None
                    and isinstance(card.purchase_kopeks, int) and card.purchase_kopeks > 0]
        if sum(int(card.available) for card, _ in choices) < quantity:
            raise SelectionError('bundle_component_unavailable:' + code)
        options[code] = choices
    pairs = sorted({(card.supplier_id, card.brand)
                    for choices in options.values() for card, _ in choices})
    best = None
    codes = sorted(needs)
    for size in (1, 2):
        for pairset in combinations(pairs, size):
            selected = []
            for code in codes:
                usable = [(card, is_own) for card, is_own in options[code]
                          if (card.supplier_id, card.brand) in pairset]
                # Fill order-warehouse stock first. A component can be split
                # across cards, but the full kit still uses at most two pairs.
                usable.sort(key=lambda x: (not x[1], tier(x[0]),
                                              x[0].purchase_kopeks or 10**15, x[0].id))
                remaining = needs[code]
                for card, is_own in usable:
                    take = min(remaining, int(card.available))
                    if take > 0:
                        selected.append((card, take, is_own))
                        remaining -= take
                    if remaining == 0:
                        break
                if remaining:
                    break
            if sum(q for _, q, _ in selected) != sum(needs.values()):
                continue
            used = {(c.supplier_id, c.brand) for c, _, _ in selected}
            if len(used) > 2:
                continue
            remote_count = sum(int(q) for _, q, is_own in selected if not is_own)
            score = (remote_count, len(used),
                     tuple(sorted(tier(c) for c, _, _ in selected)),
                     sum((c.purchase_kopeks or 10**15) * int(q) for c, q, _ in selected),
                     remote_count, tuple(c.id for c, _, _ in selected))
            if best is None or score < best[0]:
                best = (score, selected)
    if best is None:
        raise SelectionError('bundle_exceeds_two_supplier_brand_pairs')
    return best[1]
