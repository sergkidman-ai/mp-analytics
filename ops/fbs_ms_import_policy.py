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
from ops.ms_supplier_priorities import supplier_priority, choose_with_premium


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


def priority(card: Card) -> int | None:
    return supplier_priority(card.supplier_id)


def _choose_priced(cards):
    return choose_with_premium(cards, priority=priority,
                              price=lambda c: c.purchase_kopeks, identity=lambda c: c.id)


def choose_card(cards: list[Card], external_code: str, quantity: Decimal,
                remote: list[Card] | None = None) -> tuple[Card, bool]:
    """Own stock first; otherwise verified supplier stock with priority + 20%."""
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise SelectionError('invalid_quantity')
    exact = _verified(cards, external_code)
    in_stock = [c for c in exact if c.available >= quantity and priority(c) is not None]
    if in_stock:
        return sorted(in_stock, key=lambda c: (-c.available, priority(c), c.id))[0], True
    source = _verified(remote, external_code) if remote else []
    priced = [c for c in source if priority(c) is not None and c.available >= quantity
              and type(c.purchase_kopeks) is int and c.purchase_kopeks > 0]
    if not priced:
        raise SelectionError('current_purchase_price_missing')
    return _choose_priced(priced), False


class AllocationPool:
    """Track locally promised units until MS stock/reserves can be reread.

    The caller must create one pool per warehouse and discard it on any failed
    or uncertain POST. A new customerorder may not immediately create a reserve,
    so rereading MS alone cannot protect the next import in the same batch.
    """

    def __init__(self):
        self.promised: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    def choose(self, cards: list[Card], external_code: str,
               quantity: Decimal, remote: list[Card] | None = None) -> tuple[Card, bool]:
        from dataclasses import replace

        adjusted = [replace(c, reserve=c.reserve + self.promised[('own', c.id)]) for c in cards]
        remote_adjusted = [replace(c, reserve=c.reserve + self.promised[('remote', c.id)])
                           for c in (remote or [])]
        chosen, available = choose_card(adjusted, external_code, quantity, remote_adjusted)
        originals = cards if available else remote
        original = next(c for c in originals if c.id == chosen.id)
        self.promised[('own' if available else 'remote', chosen.id)] += Decimal(quantity)
        return original, available


def choose_bundle(components: list[tuple[str, Decimal]], cards: list[Card]) -> list[tuple[Card, Decimal]]:
    """Complete own-stock kit, at most two supplier-brand pairs."""
    return [(card, quantity) for card, quantity, _ in choose_bundle_plan(components, cards, [])]


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
                   if card.available >= 1 and priority(card) is not None]
        choices += [(card, False) for card in remote_exact
                    if card.available >= 1 and priority(card) is not None
                    and type(card.purchase_kopeks) is int and card.purchase_kopeks > 0]
        if sum(int(card.available) for card, _ in choices) < quantity:
            raise SelectionError('bundle_component_unavailable:' + code)
        options[code] = choices
    pairs = sorted({(card.supplier_id, card.brand)
                    for choices in options.values() for card, _ in choices})
    plans = []
    codes = sorted(needs)
    for size in (1, 2):
        for pairset in combinations(pairs, size):
            selected = []
            for code in codes:
                usable = [(card, is_own) for card, is_own in options[code]
                          if (card.supplier_id, card.brand) in pairset]
                # Fill order-warehouse stock first. A component can be split
                # across cards, but the full kit still uses at most two pairs.
                own_usable = sorted((c for c, own in usable if own),
                                    key=lambda c: (-c.available, priority(c), c.id))
                remote_usable = [c for c, own in usable if not own]
                ranked = [(c, True) for c in own_usable]
                while remote_usable:
                    card = _choose_priced(remote_usable)
                    ranked.append((card, False))
                    remote_usable.remove(card)
                remaining = needs[code]
                for card, is_own in ranked:
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
            plans.append((remote_count, len(used), selected))
    if not plans:
        raise SelectionError('bundle_exceeds_two_supplier_brand_pairs')
    structure = min((count, pairs) for count, pairs, _ in plans)
    feasible = [plan for count, pairs, plan in plans if (count, pairs) == structure]
    selected = choose_with_premium(feasible,
        priority=lambda plan: tuple(sorted(priority(c) for c, q, own in plan
                                          if not own for _ in range(int(q)))),
        price=lambda plan: sum(c.purchase_kopeks * int(q) for c, q, own in plan if not own),
        identity=lambda plan: tuple(c.id for c, _, _ in plan))
    return selected
