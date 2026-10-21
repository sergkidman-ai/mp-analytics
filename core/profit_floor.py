# поток: fin
"""Shared profit target; cost is the full cost of ``units`` pricing units.

Callers retain their established quantity/BOM semantics. No marketplace I/O.
"""
from decimal import Decimal, localcontext

POLICY_VERSION = 'profit-floor-200-through-150-v1'
POLICY_LABEL = '200 ₽ при себестоимости ≤150 ₽/ед., иначе max(300 ₽/ед.; 10% себестоимости)'


def minimum_profit(cost, units=1):
    cost, units = Decimal(str(cost)), Decimal(str(units))
    if not cost.is_finite() or not units.is_finite() or cost <= 0 or units <= 0:
        raise ValueError('Invalid profit floor input')
    with localcontext() as ctx:
        ctx.prec = 50
        # Compare totals exactly: neither round unit cost nor divide at the boundary.
        absolute = Decimal(200) if cost <= Decimal(150) * units else Decimal(300)
        return max(absolute * units, Decimal('.1') * cost)
