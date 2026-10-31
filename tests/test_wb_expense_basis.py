from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from shipment_guard.wb_expense_basis import candidate
from customer_guard.domain import business_date


def snapshot():
    lines = {key: [value] for key, value in {
        'own_price': 1000, 'to_pay': 700, 'returns_pay': 10,
        'delivery': 50, 'storage': 5, 'acceptance': 0,
        'ads': 20, 'points': 5, 'penalty': 0, 'other': 0,
        'compensation': 0,
    }.items()}
    return {'period_keys': ['2026-09'], 'provisional': [],
            'accounts': {'wb_acc1': {'lines': lines}}}


def test_september_basis_covers_all_of_october():
    rule = candidate(snapshot(), 'wb_acc1', date(2026, 10, 31))
    assert rule['base_to'] == date(2026, 9, 30)
    assert rule['valid_until'] == date(2026, 10, 31)
    assert rule['retention'] == Decimal('0.37')


def test_stale_or_provisional_basis_is_rejected():
    with pytest.raises(ValueError, match='closed_wb_month_unavailable'):
        candidate(snapshot(), 'wb_acc1', date(2026, 11, 1))
    data = snapshot()
    data['provisional'] = ['2026-09']
    with pytest.raises(ValueError, match='closed_wb_month_unavailable'):
        candidate(data, 'wb_acc1', date(2026, 10, 31))


def test_rule_expires_at_moscow_midnight():
    assert business_date(datetime(2026, 10, 31, 20, 59, tzinfo=timezone.utc)) == date(2026, 10, 31)
    assert business_date(datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)) == date(2026, 11, 1)
