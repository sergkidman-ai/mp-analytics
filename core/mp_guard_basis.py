# поток: fin
"""Explicit guard basis approved by the user on 2026-11-09.

A newly closed report does not advance this policy without a new instruction.
Market uses the fixed rate independently of its finance active.json.
"""
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

APPROVED_MONTH = '2026-10'
MARKET_RETENTION = Decimal('0.72')


def ozon_basis(account):
    from core.ozon_expense_basis import get_basis, BasisUnavailable, month_bounds
    basis = get_basis(account, require_current=False)
    first, last = month_bounds(APPROVED_MONTH)
    if (basis['period_from'], basis['period_to']) != (str(first), str(last)):
        raise BasisUnavailable('approved_ozon_guard_month_missing')
    return basis


def wb_rules():
    from shipment_guard.wb_expense_basis import candidate, ACCOUNTS
    path = Path(__file__).resolve().parents[1] / 'reports/data/mp_wb_hist.json'
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    year, month = map(int, APPROVED_MONTH.split('-'))
    publication = date(year + (month == 12), month % 12 + 1, 1)
    rules = {account: candidate(snapshot, account, publication) for account in ACCOUNTS}
    for rule in rules.values():
        rule['source_sha256'] = hashlib.sha256(raw).hexdigest()
    return rules
