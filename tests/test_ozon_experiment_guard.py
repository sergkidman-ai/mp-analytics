# поток: mkt / ozon
from decimal import Decimal as D
import hashlib
import json
import pytest
from ops.ozon_experiment_guard import Api, decision, validate_bindings, reference_week, weekly_budget_signal
import datetime as dt


POLICY = {'zero_orders_spend_rub': 300, 'emergency_without_seller_orders_rub': 500, 'automatic_stop_rub': 1000}


def test_zero_order_stop_requires_spend_and_checks_halo():
    assert decision(D(299), 0, 0, 0, 0, True, POLICY) is None
    assert decision(D(300), 0, 0, 0, 0, True, POLICY) == 'ZERO_OWN_AND_HALO_ORDERS'
    assert decision(D(800), 0, 0, 0, 1, True, POLICY) is None
    assert decision(D(800), 0, 1, 0, 0, True, POLICY) is None


def test_unknown_general_orders_are_not_zero():
    assert decision(D(300), 0, 0, 0, 0, False, POLICY) is None
    assert decision(D(500), 0, 0, 0, 0, False, POLICY) == 'PROVISIONAL_STOP_SELLER_ORDERS_UNAVAILABLE'
    assert decision(D(1000), 1, 0, 0, 0, False, POLICY) is None
    assert decision(D(1000), 0, 0, 1, 0, False, POLICY) is None


def test_only_confirmed_thousand_zero_orders_is_stop_level():
    assert decision(D(999), 0, 0, 0, 0, True, POLICY) == 'ZERO_OWN_AND_HALO_ORDERS'
    assert decision(D(1000), 0, 0, 0, 0, True, POLICY) == 'ZERO_CONFIRMED_ORDERS_1000'
    assert decision(D(2000), 0, 0, 0, 0, False, POLICY) == 'PROVISIONAL_STOP_SELLER_ORDERS_UNAVAILABLE'
    assert decision(D(2000), 0, 0, 0, 1, True, POLICY) is None


def test_guard_cannot_activate_or_stop_unrelated_campaign():
    api = Api.__new__(Api)
    api.ids = {'200'}
    with pytest.raises(AssertionError): api.call('POST', '/api/client/campaign/200/activate')
    with pytest.raises(AssertionError): api.call('POST', '/api/client/campaign/100/deactivate')
    with pytest.raises(AssertionError): api.call('POST', '/api/client/campaign/200/products/delete', {'sku': ['1']})


def test_owner_week_reference_uses_moscow_calendar_periods():
    anchor = '2026-11-06T11:30:00+00:00'
    assert tuple(map(str, reference_week(anchor, dt.datetime.fromisoformat('2026-11-12T20:59:59+00:00')))) == ('2026-11-06','2026-11-12')
    assert tuple(map(str, reference_week(anchor, dt.datetime.fromisoformat('2026-11-12T21:00:00+00:00')))) == ('2026-11-13','2026-11-19')


def test_weekly_reference_exceedance_signals_without_stopping(monkeypatch, tmp_path):
    import ops.ozon_experiment_guard as module
    now = dt.datetime.fromisoformat('2026-11-06T12:00:00+00:00')
    monkeypatch.setattr(module, 'utcnow', lambda: now)
    events = tmp_path / 'events.jsonl'
    events.write_text(json.dumps({'wave': 1, 'phase': 'RUNNING_CONFIRMED', 'at_utc': '2026-11-06T11:30:00+00:00'}) + '\n')
    class FakeApi:
        def call(self, method, path, body=None, params=None):
            assert method == 'GET' and path == '/api/client/statistics/daily/json'
            assert params['campaignIds'] == ['200']
            return {'rows': [{'id': '200', 'moneySpent': '12501.00'}]}
    cfg = {'activation_events_path': str(events), 'owner_weekly_reference_rub': 12500, 'runtime_dir': str(tmp_path)}
    binding = {'campaigns': [{'wave': 1, 'campaign_id': '200'}, {'wave': 2, 'campaign_id': '201'}]}
    state = {}; latest = {'account_unique_orders': 0, 'account_ordered_value_rub': '0', 'actions': []}
    weekly_budget_signal(FakeApi(), binding, cfg, state, latest, execute=False)
    assert latest['wave1_weekly_budget']['status'] == 'EXCEEDED'
    assert latest['wave1_weekly_budget']['automatic_budget_pause'] is False
    assert latest['actions'] == []
    assert latest['wave1_weekly_budget']['delivery']['error'] == 'dry_run'


def test_existing_campaign_and_modified_bindings_cannot_be_targeted(tmp_path):
    p = tmp_path / 'bindings.json'
    b = {'account': 'oz_acc1', 'all_12_inactive_verified': True,
         'forbidden_existing_campaign_ids': ['100'],
         'campaigns': [{'campaign_id': str(100 + i)} for i in range(12)]}
    p.write_text(json.dumps(b))
    cfg = {'bindings_path': str(p), 'bindings_sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    with pytest.raises(AssertionError): validate_bindings(cfg)
    b['campaigns'] = [{'campaign_id': str(200 + i)} for i in range(12)]
    p.write_text(json.dumps(b)); cfg['bindings_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
    assert len(validate_bindings(cfg)['campaigns']) == 12
    p.write_text(p.read_text() + '\n')
    with pytest.raises(AssertionError): validate_bindings(cfg)
