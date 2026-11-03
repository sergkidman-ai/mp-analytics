# поток: mkt
"""Fail-closed promotion scope from the current MS catalog and verified kit compositions."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from core import db

SET_WORD = re.compile(r'\b(?:набор\w*|комплект\w*|kit|set|bundle)\b', re.IGNORECASE)
BUNDLE_CODE = re.compile(r'^\d{4}[xх]\d{1,2}(?:$|\D)', re.IGNORECASE)
SET_SUFFIX = re.compile(r'^\d{4}msk', re.IGNORECASE)
BASE_CODE = re.compile(r'^\d{4}')
MIN_PRODUCTS = 30000
MSK = timezone(timedelta(hours=3))


def ordinary_codes() -> set[str]:
    """A code is usable only when the full MS catalog contains no set variant."""
    count = db.query('SELECT count(*) AS n FROM ms_product WHERE NOT archived')[0]['n']
    if count < MIN_PRODUCTS:
        raise RuntimeError(f'promo_ordinary_catalog_incomplete:{count}')
    stock_day = db.query('SELECT max(captured_at) AS day FROM supplier_stock')[0]['day']
    if stock_day != datetime.now(MSK).date():
        raise RuntimeError(f'promo_stock_snapshot_stale:{stock_day}')
    rows = db.query("""SELECT left(external_code, 4) AS code,
                              bool_or(name ~* '\\m(набор[[:alpha:]]*|комплект[[:alpha:]]*|kit|set|bundle)\\M'
                                      OR external_code ~* '^[0-9]{4}msk') AS is_set
                       FROM ms_product
                       WHERE external_code ~ '^[0-9]{4}'
                       GROUP BY 1""")
    identified_sets = {r['external_code'] for r in db.query(
        'SELECT external_code FROM set_cost WHERE components IS NOT NULL')}
    return {r['code'] for r in rows if not r['is_set'] and r['code'] not in identified_sets}


def verified_set_codes() -> set[str]:
    """Kits with a complete, four-digit component list and MS purchase coverage."""
    rows = db.query("""SELECT external_code, components, n_components FROM set_cost
                       WHERE covered = n_components AND n_components > 0""")
    return {r['external_code'] for r in rows
            if re.fullmatch(r'\d{4}', r['external_code'] or '')
            and len(r['components'] or []) == r['n_components']
            and all(re.fullmatch(r'\d{4}', c or '') for c in r['components'])}


def confirmed_set(offer_code: str | None, known_sets: set[str], name: str | None = None) -> bool:
    """Only an exact kit code or its explicitly marked kit variant is eligible."""
    raw = str(offer_code or '').strip()
    m = BASE_CODE.match(raw)
    return bool(m and m.group() in known_sets and
                (raw == m.group() or re.fullmatch(r'\d{4}msk', raw, re.I)
                 or re.fullmatch(r'\d{4}[xх]\d{1,2}', raw, re.I)
                 or SET_WORD.search(str(name or ''))))


def confirmed(offer_code: str | None, name: str | None, allowed: set[str]) -> bool:
    raw = str(offer_code or '').strip()
    match = BASE_CODE.match(raw)
    return bool(match and match.group() in allowed and
                not looks_like_set(raw, name))


def looks_like_set(offer_code: str | None, name: str | None) -> bool:
    raw = str(offer_code or '').strip()
    return bool(BUNDLE_CODE.match(raw) or SET_SUFFIX.match(raw) or
                SET_WORD.search(str(name or '')) or SET_WORD.search(raw))


def confirmed_wb_cost(vendor, cost, allowed, known_sets):
    """WB price optimization requires a complete kit or a verified bundle multiplier."""
    if not cost.get('cogs') or cost.get('branch') in ('none', 'unmapped', 'no_mp_stock'):
        return False
    raw = str(vendor or '').strip()
    match = re.match(r'^\d{4}[xх](\d{1,2})(?=\D|$)', raw, re.I)
    if match and int(match.group(1)) != int(cost.get('bundle', 1)):
        return False
    if cost.get('is_set'):
        return confirmed_set(raw, known_sets, cost.get('title'))
    if match:
        return raw[:4] in allowed and int(cost.get('bundle', 1)) > 1
    return confirmed(cost.get('external_code') or raw, cost.get('title'), allowed)
