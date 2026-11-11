# поток: fin
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
import json
import pytest
from psycopg2.extras import Json

from core import db
from core.ozon_expense_basis import BasisUnavailable
from customer_guard import rules
from customer_guard.domain import economics
from tests.test_customer_guard import pg, posting, NOW


@pytest.mark.parametrize('account', ['oz_acc1', 'oz_acc2'])
def test_explicit_september_pin_is_local_to_guard_and_works_in_november(pg, monkeypatch, account):
    payload = dict(account=account, status='complete', period_from='2026-09-01', period_to='2026-09-30',
                   retention='0.60', keep='0.40', version='original-september', source_sha256='verified')
    db.execute("INSERT INTO fin_ozon_expense_basis(version,account,period_from,period_to,status,payload) VALUES(%s,%s,%s,%s,%s,%s)",
               (payload['version'], account, '2026-09-01', '2026-09-30', 'complete', Json(payload)))
    db.execute("UPDATE customer_guard_runtime SET settings=settings || '{\"expense_month\":\"2026-09\"}'::jsonb")
    global_rule = Mock(side_effect=AssertionError('global current rule must not be used or extended'))
    monkeypatch.setattr(rules, 'shipment_rule', global_rule)
    rule = rules.get_rule(account, as_of=date(2026, 11, 3))
    assert rule['retention'] == Decimal('.60') and rule['base_to'] == date(2026, 9, 30)
    assert rule['basis_pinned'] and rule['valid_until'] >= date(2026, 11, 3)
    assert rules.expense_basis(account)['version'] == rule['version']
    assert not db.query('SELECT * FROM fin_ozon_expense_current')
    post = posting();post['account'] = account
    value = economics(post, Decimal(150), 'actual', rule, NOW)
    assert value['floor'] == '875.00' and value['estimated_net'] == '250.00'


def test_missing_confirmed_pinned_basis_fails_closed(pg):
    db.execute("UPDATE customer_guard_runtime SET settings=settings || '{\"expense_month\":\"2026-09\"}'::jsonb")
    with pytest.raises(BasisUnavailable, match='confirmed_pinned_ozon_basis_missing'):
        rules.get_rule('oz_acc1')


def test_wb_september_uses_existing_frozen_snapshot(pg, monkeypatch, tmp_path):
    from shipment_guard.wb_expense_basis import candidate
    from tests.test_wb_expense_basis import snapshot
    data = snapshot()
    target = tmp_path / 'reports/data';target.mkdir(parents=True)
    (target/'mp_wb_hist.json').write_text(json.dumps(data))
    monkeypatch.setattr(rules, '__file__', str(tmp_path/'customer_guard/rules.py'))
    db.execute("UPDATE customer_guard_runtime SET settings=settings || '{\"expense_month\":\"2026-09\"}'::jsonb")
    rule = rules.get_rule('wb_acc1', as_of=date(2026, 11, 3))
    assert rule['retention'] == candidate(data, 'wb_acc1', date(2026, 10, 1))['retention']
    assert rule['base_from'] == date(2026, 9, 1) and rule['basis_pinned']
    assert rule['valid_until'] >= date(2026, 11, 3)
    data['provisional'] = ['2026-09']
    (target/'mp_wb_hist.json').write_text(json.dumps(data))
    with pytest.raises(BasisUnavailable, match='confirmed_pinned_wb_basis_missing'):
        rules.get_rule('wb_acc1')


def test_no_pin_keeps_existing_current_month_policy(pg, monkeypatch):
    current = Mock(return_value={'version':'current-month'})
    monkeypatch.setattr(rules, 'shipment_rule', current)
    assert rules.get_rule('oz_acc1')['version'] == 'current-month'
    assert current.call_count == 1


def test_market_stays_fixed_72_without_any_financial_basis(monkeypatch):
    monkeypatch.setattr(rules.db, 'query', Mock(side_effect=AssertionError('no finance lookup')))
    rule = rules.get_rule('ya_acc1', as_of=date(2026, 11, 3))
    assert rule['retention'] == Decimal('.72') and rule['base_from'] is None
