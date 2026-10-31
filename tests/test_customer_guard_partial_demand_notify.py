# поток: fin
"""Incomplete shipment coverage alerts both recipients once, then resolves."""
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

from core import db
from customer_guard import store, notifications as primary, extra_loss_notify as extra
from tests.test_customer_guard import pg, posting, ingest, NOW
from tests.test_customer_guard_runtime import activate


def check_partial(number='123-001-1', now=NOW + timedelta(minutes=10), reason=True):
    post = posting(number, status='delivering')
    if not db.query('SELECT id FROM customer_guard_orders WHERE posting_number=%s', (number,)):
        ingest([post])
    job = store.claim(now, number)
    assert job and store.refresh(job, post, now)
    document = {'id': str(uuid4()), 'state': {'name': 'В пути'},
                'demands': [{'meta': {'href': 'https://ms.invalid/demand/123'}}]}
    result = {'signal': 'unknown' if reason else 'ok',
              'reason': 'partial_demand_coverage' if reason else None,
              'positions': [{'code': '5630sf', 'external_code': '5630', 'quantity': 2}],
              'cost': None if reason else '100', 'cost_kind': None if reason else 'actual'}
    assert store.finish(job, result, now, document)
    return job['order_id']


def test_partial_shipment_alerts_natalia_and_dragon_then_resolves(pg, monkeypatch):
    activate()
    at = NOW + timedelta(minutes=10)
    oid = check_partial(now=at)
    monkeypatch.setattr(primary, 'transport', lambda: ('https://unused.invalid', 'natalia'))
    monkeypatch.setattr(primary, 'utcnow', lambda: at)
    monkeypatch.setattr(primary, 'get_basis', lambda account: {'version': 'irrelevant'})
    monkeypatch.setattr(extra, 'settings', lambda: ('dragon', 'blackdragon2233', NOW))
    monkeypatch.setattr(extra, 'checked_transport', lambda *args: 'https://unused.invalid')
    post = Mock(side_effect=[
        Mock(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 101}}),
        Mock(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 102}}),
    ])
    monkeypatch.setattr(primary.requests, 'post', post)

    assert primary.send_current(oid) == 'sent'
    assert extra.send_pending(now=at)['sent'] == 1
    assert [call.kwargs['json']['chat_id'] for call in post.call_args_list] == ['natalia', 'dragon']
    assert all('ОТГРУЗКА НЕ ПОКРЫВАЕТ ЗАКАЗ' in call.kwargs['json']['text']
               for call in post.call_args_list)
    assert primary.send_current(oid) == 'none'
    assert extra.send_pending(now=at)['sent'] == 0

    assert store.schedule_rechecks(at + timedelta(minutes=120)) == 1
    check_partial(now=at + timedelta(minutes=120))
    assert primary.send_current(oid) == 'none'
    assert extra.send_pending(now=at + timedelta(minutes=120))['sent'] == 0
    assert store.schedule_rechecks(at + timedelta(minutes=240)) == 1
    check_partial(now=at + timedelta(minutes=240), reason=False)
    incident = db.query("SELECT resolved_at,delivery FROM customer_guard_incidents WHERE kind='partial_demand_coverage'")[0]
    assert incident['resolved_at'] is not None and incident['delivery'] == 'sent'


def test_no_partial_alert_without_demand_link(pg):
    activate()
    post = posting(status='delivering')
    ingest([post])
    at = NOW + timedelta(minutes=10)
    job = store.claim(at)
    assert store.refresh(job, post, at)
    result = {'signal': 'unknown', 'reason': 'partial_demand_coverage'}
    assert store.finish(job, result, at, {'id': str(uuid4()), 'state': {'name': 'В пути'}, 'demands': []})
    assert not db.query("SELECT id FROM customer_guard_incidents WHERE kind='partial_demand_coverage'")
