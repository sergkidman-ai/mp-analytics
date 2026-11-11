# поток: fin
from datetime import timedelta
from unittest.mock import Mock
from psycopg2.extras import Json

from core import db
from customer_guard import store, notifications as n
from tests.test_customer_guard import pg, posting, ingest, NOW
from tests.test_customer_guard_runtime import activate, fake_transport


def test_explicit_batch_can_notify_before_delay_without_changing_first_seen(pg, monkeypatch):
    activate();ingest([posting()])
    row = db.query('SELECT id,first_seen_at,ms_due_at FROM customer_guard_orders')[0]
    at = NOW + timedelta(minutes=1)
    db.execute('UPDATE customer_guard_jobs SET due_at=%s', (at,))
    job = store.claim(at)
    assert store.refresh(job, posting(), at)
    result = dict(signal='below_floor', price='100', cost='100', floor='1000',
                  estimated_net='-10', rule_version='test-basis', cost_kind='actual')
    document = {'id':'a385a0f9-d769-11f1-0a80-1ff300278691', 'state':{'name':'Собирается'}}
    assert store.finish(job, result, at, document)
    fake_transport(monkeypatch, at)
    sender = Mock(return_value=Mock(status_code=200, json=lambda:{'ok':True,'result':{'message_id':1}}))
    monkeypatch.setattr(n.requests,'post',sender)
    assert n.send_current(row['id']) == 'stale_or_disabled'
    settings={'immediate_order_ids':[row['id']], 'immediate_until':(at+timedelta(hours=3)).isoformat()}
    db.execute('UPDATE customer_guard_runtime SET settings=settings || %s::jsonb', (Json(settings),))
    assert n.send_current(row['id']) == 'sent' and sender.call_count == 1
    after=db.query('SELECT first_seen_at,ms_due_at FROM customer_guard_orders')[0]
    assert after['first_seen_at']==row['first_seen_at'] and after['ms_due_at']==row['ms_due_at']
    db.execute("UPDATE customer_guard_runtime SET settings=settings-'immediate_order_ids'-'immediate_until'")
