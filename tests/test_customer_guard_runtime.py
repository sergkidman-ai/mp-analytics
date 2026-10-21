# поток: fin
from datetime import timedelta
from uuid import uuid4
from unittest.mock import Mock

import requests
import pytest
from core import db
from customer_guard import store, notifications as n, runtime
from tests.test_customer_guard import pg, posting, ingest, NOW


def activate():
    db.execute("""UPDATE customer_guard_runtime SET enabled=true,notifications_enabled=true,
      activated_at=%s,heartbeat_at=%s WHERE id""",(NOW,NOW))


def checked(number="123-001-1", status="awaiting_packaging", ms_state="Подтвержден", signal="below_floor",
            cost_kind="fifo_estimate", net="-0.01", version="test-basis"):
    post=posting(number,status)
    ingest([post])
    at=NOW+timedelta(minutes=10)
    job=store.claim(at,number)
    assert store.refresh(job,post,at)
    document={"id":str(uuid4()),"state":{"name":ms_state}} if ms_state else None
    result=dict(signal=signal,cost_kind=cost_kind,price="728",cost="69.28",
                floor="941.85",gap="-213.85",estimated_net=net,rule_version=version)
    assert store.finish(job,result,at,document)
    return job["order_id"],at


def fake_transport(monkeypatch, at):
    monkeypatch.setattr(n,"transport",lambda:("https://unused.invalid","natalia-only"))
    monkeypatch.setattr(n,"utcnow",lambda:at)
    monkeypatch.setattr(n,"get_basis",lambda account:{"version":"test-basis"})


def test_hourly_rechecks_do_not_poll_shipped_or_shorten_retry(pg):
    ready,at=checked(signal="ok",ms_state="Подтвержден",cost_kind="fifo_estimate")
    shipped,_=checked("123-001-2",status="delivering",signal="ok",ms_state="В пути",cost_kind="actual")
    assert store.schedule_rechecks(at+timedelta(minutes=59,seconds=59))==0
    assert store.schedule_rechecks(at+timedelta(minutes=60))==1
    assert store.schedule_rechecks(at+timedelta(minutes=61))==0
    states={r["order_id"]:r["state"] for r in db.query("SELECT order_id,state FROM customer_guard_jobs")}
    assert states=={ready:"pending",shipped:"done"}
    job=store.claim(at+timedelta(minutes=61))
    store.fail(job,"ms_429",at+timedelta(minutes=61),600)
    assert store.schedule_rechecks(at+timedelta(minutes=65))==0
    assert store.claim(at+timedelta(minutes=70)) is None


def test_initial_pre_shipment_checks_are_not_stuck_behind_historical_deliveries(pg):
    ingest([posting("old-001-1",status="delivering"),posting("new-001-1")])
    job=store.claim(NOW+timedelta(minutes=10))
    assert job["order"]["posting_number"]=="new-001-1"


def test_sent_once_to_single_recipient(pg,monkeypatch):
    activate();oid,at=checked()
    fake_transport(monkeypatch,at)
    post=Mock(return_value=Mock(status_code=200,json=lambda:{"ok":True,"result":{"message_id":123}}))
    monkeypatch.setattr(n.requests,"post",post)
    assert n.send_current(oid)=="sent"
    assert n.send_current(oid)=="none"
    assert post.call_count==1
    body=post.call_args.kwargs["json"]
    assert body["chat_id"]=="natalia-only" and "Расчётная чистая прибыль: -0.01 ₽" in body["text"]
    assert db.query("SELECT telegram_message_id FROM customer_guard_incidents")[0]["telegram_message_id"]==123


def test_timeout_uncertain_and_crash_recovery_never_resends(pg,monkeypatch):
    activate();oid,at=checked();fake_transport(monkeypatch,at)
    post=Mock(side_effect=requests.Timeout("must not log request URL"))
    monkeypatch.setattr(n.requests,"post",post)
    assert n.send_current(oid)=="uncertain"
    assert n.send_current(oid)=="none"
    assert post.call_count==1
    db.execute("UPDATE customer_guard_incidents SET delivery='sending',attempted_at=%s",(at,))
    n.recover_interrupted(at+timedelta(minutes=3))
    assert db.query("SELECT delivery FROM customer_guard_incidents")[0]["delivery"]=="uncertain"


@pytest.mark.parametrize("condition",["stale","cancelled","disabled","before_activation"])
def test_no_delivery_for_stale_cancelled_or_disabled(pg,monkeypatch,condition):
    activate();oid,at=checked()
    fake_transport(monkeypatch,at+timedelta(minutes=2) if condition=="stale" else at)
    if condition=="cancelled":ingest([posting(status="cancelled")],at+timedelta(seconds=1))
    if condition=="disabled":db.execute("UPDATE customer_guard_runtime SET notifications_enabled=false")
    if condition=="before_activation":db.execute("UPDATE customer_guard_runtime SET activated_at=%s",(at+timedelta(seconds=1),))
    post=Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(n.requests,"post",post)
    n.send_current(oid)
    assert not post.called


def test_cancel_between_claim_and_send(pg,monkeypatch):
    activate();oid,at=checked();fake_transport(monkeypatch,at)
    original=n.claim
    def claim(*args):
        row=original(*args)
        ingest([posting(status="cancelled")],at+timedelta(seconds=1))
        return row
    monkeypatch.setattr(n,"claim",claim)
    post=Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(n.requests,"post",post)
    assert n.send_current(oid)=="suppressed"
    assert not post.called


@pytest.mark.parametrize("net,expected",[("-100.60",True),("0",True),("0.01",False),
    ("216.15",False),(None,False),("NaN",False),("Infinity",False),(True,False)])
def test_profit_threshold_and_message(pg,monkeypatch,net,expected):
    activate();oid,at=checked(net=net)
    fake_transport(monkeypatch,at)
    post=Mock(return_value=Mock(status_code=200,json=lambda:{"ok":True,"result":{"message_id":7}}))
    monkeypatch.setattr(n.requests,"post",post)
    assert n.send_current(oid)==("sent" if expected else "none")
    assert post.called==expected
    if expected:
        text=post.call_args.kwargs["json"]["text"]
        assert "Расчётная чистая прибыль:" in text and "по средней доле кабинета" in text
        assert "предварительная оценка" in text


@pytest.mark.parametrize("kwargs",[
    {"status":"delivering","ms_state":"В пути"},
    {"ms_state":"Ожидает подтверждения"},
    {"signal":"missing_ms","ms_state":None,"net":None},
    {"version":None},
])
def test_no_alert_outside_ready_orders_and_complete_calculation(pg,kwargs):
    checked(**kwargs)
    assert not db.query("SELECT * FROM customer_guard_incidents")


@pytest.mark.parametrize("mode",["stale_basis","unavailable_basis","legacy_kind","basis_changed_before_send"])
def test_sender_rechecks_policy_and_expense_basis(pg,monkeypatch,mode):
    activate();oid,at=checked();fake_transport(monkeypatch,at)
    if mode=="legacy_kind":db.execute("UPDATE customer_guard_incidents SET kind='below_floor'")
    if mode=="stale_basis":monkeypatch.setattr(n,"get_basis",lambda account:{"version":"other"})
    if mode=="unavailable_basis":monkeypatch.setattr(n,"get_basis",Mock(side_effect=n.BasisUnavailable("missing")))
    if mode=="basis_changed_before_send":
        monkeypatch.setattr(n,"get_basis",Mock(side_effect=[{"version":"test-basis"},{"version":"other"}]))
    post=Mock(side_effect=AssertionError("must not send"))
    monkeypatch.setattr(n.requests,"post",post)
    n.send_current(oid)
    assert not post.called


def test_tick_disabled_no_network_and_ten_minute_sync_gate(pg,monkeypatch):
    # Advisory locks are database-wide even when fixture tables use a private schema.
    monkeypatch.setattr(runtime,"RUNTIME_LOCK",uuid4().int % (2**63-1))
    sync=Mock(return_value={})
    monkeypatch.setattr(runtime.service,"sync",sync)
    assert runtime.tick()=={"enabled":False}
    assert not sync.called
    activate()
    ingest([posting()])
    monkeypatch.setattr(runtime,"utcnow",lambda:NOW+timedelta(minutes=9))
    monkeypatch.setattr(runtime.service,"refresh",Mock(return_value={}))
    monkeypatch.setattr(runtime.service,"work",Mock(return_value={}))
    monkeypatch.setattr(runtime.service,"reprice",Mock(return_value={}))
    runtime.tick()
    assert not sync.called
    monkeypatch.setattr(runtime,"utcnow",lambda:NOW+timedelta(minutes=10))
    runtime.tick()
    assert sync.call_count==1


def test_message_has_selected_product_codes_and_bounded_length():
    row=dict(kind='non_positive_profit',posting_number='123-1',ms_status='Неукомплектован',
        result=dict(price='100',cost='60',floor='900',gap='-800',estimated_net='-20',
                    cost_kind='purchase_estimate',rule_version='test',
                    positions=[dict(code='0001wb',external_code='0001',quantity=2),
                               dict(code='0002at',external_code='0002',quantity=1)]))
    text=n.message(row)
    assert 'Код МС: 0001wb; внешний код: 0001 × 2' in text
    assert 'Код МС: 0002at; внешний код: 0002 × 1' in text
    assert 'Расчётная чистая прибыль: -20.00 ₽' in text
    row['result']['positions']=[dict(code='x'*200,external_code='y'*200,quantity=1)]*100
    assert len(n.message(row))<4096 and 'Ещё 88 позиций' in n.message(row)
