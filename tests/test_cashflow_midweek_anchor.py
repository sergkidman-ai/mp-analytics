# поток: cf
import datetime as dt
from collections import defaultdict

import pytest

from web import cashflow_api as api


@pytest.mark.parametrize('anchor', [1000.0, 0.0])
def test_midweek_anchor_starts_forecast_without_repeating_fact_or_today_plan(monkeypatch, anchor):
    today = dt.date.today()
    week = api.monday(today)
    key = ('ozon', 'test')
    bal = api.Balances.__new__(api.Balances)
    bal.api, bal.manual, bal.txn, bal.mirror = [defaultdict(list) for _ in range(4)]
    bal.manual[key] = [(today, anchor)]
    bal.txn[key] = [(today, 123.0)]
    bal.first_txn, bal.last_txn = {key: today}, {key: today}
    monkeypatch.setattr(api, 'Balances', lambda org: bal)
    monkeypatch.setattr(api, 'warnings', lambda *args: [])
    queries = []

    def query(sql, params=None):
        if 'bool_or(estimate)' in sql:
            return [
                dict(week_start=week, kind='fact', category='b2b', s=123, est=False),
                dict(week_start=week, kind='plan', category='b2b', s=230, est=False),
                dict(week_start=week + dt.timedelta(weeks=1), kind='plan', category='b2b', s=50, est=False),
            ]
        if 'coalesce(sum(amount), 0)' in sql:
            queries.append((sql, params))
            assert params == ('7811803918', week, today)
            assert "kind='plan'" in sql and 'day>%s' in sql
            return [{'s': 30}]  # Only future-day plan; today's 200 is already in the EOD anchor.
        if 'max(id)' in sql:
            return [{'max': 1}]
        return []

    monkeypatch.setattr(api.db, 'query', query)
    result = api.cashflow('7811803918')['balance']
    assert result['open'][week.isoformat()] is None
    assert result['close'][week.isoformat()] == anchor + 30
    assert result['close'][(week + dt.timedelta(weeks=1)).isoformat()] == anchor + 80
    assert result['forecast_base'] == {'date': today.isoformat(), 'balance': anchor}
    assert result['exact'][week.isoformat()] is False
    assert all(v is None for k, v in result['close'].items() if k < week.isoformat())
    assert len(queries) == 1


def test_known_week_opening_preserves_existing_forecast(monkeypatch):
    today = dt.date.today()
    week = api.monday(today)

    class KnownBalances:
        def total(self, day):
            return 1000, True

        def accounts(self):
            return set()

    monkeypatch.setattr(api, 'Balances', lambda org: KnownBalances())
    monkeypatch.setattr(api, 'warnings', lambda *args: [])

    def query(sql, params=None):
        if 'bool_or(estimate)' in sql:
            return [dict(week_start=week, kind=kind, category='b2b', s=s, est=False)
                    for kind, s in [('fact', 123), ('plan', 230)]]
        if 'coalesce(sum(amount), 0)' in sql:
            pytest.fail('Existing weekly forecast must not be rebased')
        return [{'max': 1}] if 'max(id)' in sql else []

    monkeypatch.setattr(api.db, 'query', query)
    result = api.cashflow('7807355364')['balance']
    assert result['close'][week.isoformat()] == 1353
    assert result['forecast_base'] is None
