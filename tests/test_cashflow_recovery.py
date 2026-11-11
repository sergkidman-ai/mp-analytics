# поток: cf
import datetime as dt
from collections import defaultdict
import pytest
from web.cashflow_api import Balances
from cf import balances, plan_seed, mp_forecast, mp_balance

D = dt.date(2026, 11, 9)
K = ('alfa', 'test')


def balance():
    b = Balances.__new__(Balances)
    b.api, b.manual, b.txn, b.mirror = [defaultdict(list) for _ in range(4)]
    b.first_txn, b.last_txn = {}, {}
    return b


def test_no_accounts_is_unknown():
    assert balance().total(D) == (None, False)


def test_actual_zero_is_known():
    b = balance()
    b.api[K] = [(D, 0)]
    assert b.total(D) == (0, True)


def test_old_api_balance_is_not_exact():
    b = balance()
    b.api[K] = [(D - dt.timedelta(days=1), 10)]
    assert b.total(D) == (10, False)


def test_transactions_without_opening_do_not_create_money():
    b = balance()
    b.txn[K] = [(D, 100)]
    b.first_txn[K] = b.last_txn[K] = D
    assert b.at(K, D) == (None, False)


def test_partial_accounts_do_not_hide_unknown():
    b = balance()
    b.api[K] = [(D, 15)]
    b.txn[('ozon', 'unknown')] = [(D, 1)]
    assert b.total(D) == (None, False)


def test_manual_anchor_rolls_operations():
    b = balance()
    b.manual[K] = [(D - dt.timedelta(days=1), 100)]
    b.txn[K] = [(D, -20)]
    b.last_txn[K] = D
    assert b.at(K, D) == (80, True)


def test_tax_history_missing_does_not_invent_vat(monkeypatch):
    monkeypatch.setattr(plan_seed.db, 'query', lambda *a, **kw: [])
    monkeypatch.setattr(plan_seed, 'vat_from_turnover', lambda *a: pytest.fail('unagreed VAT base'))
    assert plan_seed.tax_rules(D) == []


def test_wb_missing_bank_history_is_not_overdue(monkeypatch):
    w = dt.date(2026, 9, 21)
    monkeypatch.setattr(mp_forecast, 'wb_weeks', lambda: {'wb_acc1': {w: 1000}})
    monkeypatch.setattr(mp_forecast, 'bank_credits', lambda *a: [])
    monkeypatch.setattr(mp_forecast.db, 'query', lambda *a: [{'first': D, 'last': D}])
    lines = mp_forecast.wb_forecast(D, D + dt.timedelta(days=7))
    assert not any('просрочено' in l['note'] for l in lines)


def test_wb_current_token_fallback(monkeypatch):
    monkeypatch.delenv('WB_TOKEN_FINANCE_ACC2', raising=False)
    monkeypatch.setenv('WB_TOKEN_ACC2', 'test-token')
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'current': 0}
    def get(*args, **kwargs):
        assert kwargs['headers']['Authorization'] == 'test-token'
        return Response()
    monkeypatch.setattr(mp_balance.requests, 'get', get)
    assert mp_balance.wb_balance('wb_acc2')[0] == 0


def test_empty_api_serializes_unknown_balances(monkeypatch):
    from web import cashflow_api as api
    def query(sql, params=None):
        if 'min(operation_date)' in sql:
            return [{'first': None, 'last': None}]
        if 'max(operation_date)' in sql:
            return [{'d': None}]
        if 'count(*) n' in sql:
            return [{'n': 0, 's': 0}]
        if 'max(id)' in sql:
            return [{'max': None}]
        return []
    monkeypatch.setattr(api.db, 'query', query)
    result = api.cashflow('7807355364')
    assert result['empty'] is True
    assert all(v is None for v in result['balance']['close'].values())
    assert all(v is None for v in result['balance']['diff'].values())
    assert result['accounts'] == []
