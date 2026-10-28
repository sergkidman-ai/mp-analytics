from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from core import db
from collectors.mp_return_status_history import event, record, status_fields


def test_collects_nested_marketplace_statuses_without_customer_fields():
    raw = {
        "visual": {"status": {"sys_name": "ReceivedBySeller", "display_name": "Получен"},
                   "change_moment": "2026-10-28T11:49:15Z"},
        "compensation_status": {"status": {"sys_name": "Waiting"}},
        "items": [{"logisticsStatus": "RECEIVED_ON_FULFILLMENT"}],
        "customer_name": "private",
    }
    result = status_fields(raw)
    assert result["visual.status.sys_name"] == "ReceivedBySeller"
    assert result["compensation_status.status.sys_name"] == "Waiting"
    assert result["items[0].logisticsStatus"] == "RECEIVED_ON_FULFILLMENT"
    assert "customer_name" not in result


def test_substatus_or_location_change_creates_new_fingerprint():
    head = dict(platform="ozon", account="oz_acc2", return_id="42", source="ozon",
                status_raw="MovingToSeller", status_name="Едет к вам", stage="transit")
    at = datetime(2026, 10, 28, tzinfo=timezone.utc)
    a = event(head, {"visual": {"status": {"sys_name": "MovingToSeller"}},
                     "place": {"name": "СЦ-1"}}, at)
    b = event(head, {"visual": {"status": {"sys_name": "MovingToSeller"}},
                     "place": {"name": "СЦ-2"}}, at)
    c = event(head, {"visual": {"status": {"sys_name": "ArrivedAtReturnPlace"}},
                     "place": {"name": "СЦ-1"}}, at)
    assert a["status_hash"] != b["status_hash"]
    assert a["status_hash"] != c["status_hash"]
    assert a["source_changed_at"] is None


@pytest.fixture
def isolated_events(monkeypatch):
    schema = 'return_events_test_' + uuid4().hex
    original = db.get_conn
    with original() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET LOCAL search_path TO "{schema}"')
        cur.execute((Path(__file__).parents[1] / 'migrations/080_mp_return_status_events.sql').read_text())

    @contextmanager
    def isolated():
        with original() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SET LOCAL search_path TO "{schema}"')
            yield conn

    monkeypatch.setattr(db, 'get_conn', isolated)
    try:
        yield
    finally:
        with original() as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_records_changes_and_repeated_status_cycle(isolated_events):
    head = dict(platform='ozon',account='oz_acc2',return_id='42',source='ozon',
                status_raw='MovingToSeller',status_name='Едет к вам',stage='transit')
    raw = {'visual':{'status':{'sys_name':'MovingToSeller'}}}
    at = datetime(2026,10,28,tzinfo=timezone.utc)
    assert record([(head,[],raw)],observed_at=at)==1
    assert record([(head,[],raw)],observed_at=at+timedelta(minutes=1))==0
    arrived = dict(head,status_raw='ArrivedAtReturnPlace',status_name='В ПВЗ',stage='pickup')
    assert record([(arrived,[],{'visual':{'status':{'sys_name':'ArrivedAtReturnPlace'}}})],
                  observed_at=at+timedelta(minutes=2))==1
    assert record([(head,[],raw)],observed_at=at+timedelta(minutes=3))==1
    assert [x['status_raw'] for x in db.query('SELECT status_raw FROM mp_return_status_events ORDER BY id')] == [
        'MovingToSeller','ArrivedAtReturnPlace','MovingToSeller']
