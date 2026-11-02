# поток: inv
"""Verified TC set compositions already restored into the local set_cost table."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import re

from core import db


@lru_cache(maxsize=1)
def catalog() -> dict[str, tuple[str, ...]]:
    rows = db.query('SELECT external_code,components,n_components FROM set_cost')
    result = {}
    for row in rows:
        code, items = row['external_code'], row['components'] or []
        if not re.fullmatch(r'\d{4}', code or '') or not items:
            continue
        if len(items) != row['n_components'] or any(not re.fullmatch(r'\d{4}', x or '') for x in items):
            continue
        result[code] = tuple(items)
    return result


def components(code: str, mapping: dict[str, tuple[str, ...]] | None = None) -> list[tuple[str, int]] | None:
    """Flatten nested sets; None means no verified set for this exact parent code."""
    source = catalog() if mapping is None else mapping
    if code not in source:
        return None

    def expand(parent: str, seen: frozenset[str]) -> Counter:
        if parent in seen:
            raise ValueError('set_composition_cycle:' + parent)
        if parent not in source:
            return Counter({parent: 1})
        total = Counter()
        for child in source[parent]:
            total.update(expand(child, seen | {parent}))
        return total

    return sorted(expand(code, frozenset()).items())
