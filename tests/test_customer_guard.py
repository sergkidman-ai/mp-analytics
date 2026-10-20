# поток: fin
"""Deterministic business invariants; PostgreSQL tests use an isolated schema."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from core import db
from customer_guard import domain, store
from customer_guard.service import evaluate
from customer_guard.sources import ORG
from customer_guard.sources import CustomerMS
from shipment_guard import config

NOW = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


def posting(number="123-001-1", status="awaiting_packaging"):
    return dict(posting_number=number, order_number="123-001", status=status,
                in_process_at=NOW.isoformat(),
                products=[dict(offer_id="set", sku=123, price={"amount":"1000.00","currency":"RUB"}, quantity=1)])


def position(pid="p1", qty=1):
    return dict(quantity=qty, assortment={"meta":{"href":"https://ms/entity/product/"+pid}})


def rule():
    return dict(account="oz_acc2", platform="ozon", organization_id=ORG,
                retention=Decimal(".1"), valid_until=NOW.date(), version="test", enabled=True)


def test_wire_price_does_not_change_business_revision():
    a, b = posting(), posting()
    b["products"][0]["price"] = "1000.0000"
    b["substatus"] = ""
    assert domain.posting_hash(a) == domain.posting_hash(b)
    b["products"][0]["quantity"] = 2
    assert domain.posting_hash(a) != domain.posting_hash(b)


def test_kit_is_one_sold_unit_and_fifo_not_multiplied():
    result = domain.economics(posting(), Decimal("510.08"), "fifo_estimate", rule(), NOW)
    assert result["units"] == "1"
    assert result["floor"] == "900.09"
    assert result["signal"] == "aggregate_only"  # No unproven green line allocation.
    rows = [{"positions":[{"meta":{"href":"https://ms/entity/product/p1"},"cost":12300,"quantity":999}]}]
    assert domain.fifo_cost([position(qty=3)], rows) == Decimal(123)
    with pytest.raises(ValueError):
        domain.fifo_cost([position(), position("p2")], rows)


def test_expired_rule_never_produces_floor():
    r = rule()
    r["valid_until"] -= timedelta(days=1)
    assert domain.economics(posting(), Decimal(10), "actual", r, NOW)["floor"] is None


def test_exact_kit_composition_and_selected_alternative():
    p = posting()
    a, b = position("p1"), position("p2")
    a["assortment"]["externalCode"] = "a"
    b["assortment"]["externalCode"] = "b"
    def resolver(code):
        return ({"a":1,"b":1} if code == "set" else {code:1}), {"source":"TK"}
    assert domain.composition(p, [a,b], resolver)["verified"]
    b["quantity"] = 2
    assert not domain.composition(p, [a,b], resolver)["verified"]
    single = domain.economics(p, Decimal(500), "fifo_estimate", rule(), NOW, allocation_verified=True)
    assert single["signal"] == "ok"


def test_draft_counts_as_existing_and_does_not_fetch_positions():
    class NoMS:
        def positions(self, *args):
            pytest.fail("draft should not fetch positions")
    result = evaluate(posting(), {"state":{"name":"Ожидает подтверждения"}}, NoMS(), rule(), NOW)
    assert result["signal"] == "draft"
    assert result["cost"] is None


def test_failed_cost_is_not_zero_or_green():
    class MS:
        def positions(self, *args):
            return [position()]
        def cost(self, *args):
            raise ValueError("partial_demand_coverage")
    result = evaluate(posting(), {"id":"x","state":{"name":"Собирается"}}, MS(), rule(), NOW)
    assert result["signal"] == "unknown"
    assert result["cost"] is None


def test_supplier_receipt_price_quantity_vat_and_currency():
    class MS(CustomerMS):
        def __init__(self):
            self.remote = config.MS+"/entity/store/remote"
            self.receipts = {}
            self.currency = "643"
            self.enter_count = 1
            self.position_calls = 0
        def get(self, path, params=None):
            if path.startswith("/entity/enter/"):
                return {"updated":"v1"}
            return {"article":"supplier-unique", "supplier":{"meta":{"href":"supplier"}}}
        def collection(self, path, params=None, max_pages=10):
            return [{"id":"receipt", "updated":"v1", "rate":{"currency":{"code":self.currency}},
                     "vatEnabled":True,"vatIncluded":False}] * self.enter_count
        def positions(self, entity, uid):
            self.position_calls += 1
            return [{**position(), "id":"line", "price":10000, "vat":20}]
    client = MS()
    value, proof = client.supplier_cost([position(qty=3)])
    assert value == Decimal(360)
    assert proof[0]["unit"] == "120.0"
    assert client.supplier_cost([position(qty=2)])[0] == Decimal(240)
    assert client.position_calls == 1  # Same receipt version reused within a run.
    client.currency = "840"
    with pytest.raises(ValueError, match="currency"):
        client.supplier_cost([position()])
    client.currency = "643"
    client.enter_count = 2
    with pytest.raises(ValueError, match="not_unique"):
        client.supplier_cost([position()])


def test_partial_demands_do_not_double_count_estimated_remainder():
    uid = str(uuid4())
    class MS(CustomerMS):
        def __init__(self):
            pass
        def get(self, path, params=None):
            return {"applicable":True, "updated":"v1",
                "organization":{"meta":{"href":config.MS+"/entity/organization/"+ORG}},
                "agent":{"meta":{"href":config.MS+"/entity/counterparty/b7e22828-0b7c-11ee-0a80-11480012ed47"}}}
        def positions(self, entity, uid):
            return [position(qty=1)]
        def fifo(self, entity):
            return [{"positions":[{"meta":{"href":"https://ms/entity/product/p1"},"cost":30000}]}]
    order = {"demands":[{"meta":{"href":config.MS+"/entity/demand/"+uid}}]}
    client = MS()
    with pytest.raises(ValueError, match="partial_demand_coverage"):
        client.cost(order, [position(qty=2)])
    cost, kind, proof = client.cost(order, [position(qty=1)])
    assert (cost,kind)==(Decimal(300),"actual")
    assert proof[0]["entity_id"]==uid


@pytest.fixture
def pg(monkeypatch):
    """Only customer_guard_test_<uuid> is created/dropped; no public tables modified."""
    schema = "customer_guard_test_" + uuid4().hex
    original = db.get_conn
    with original() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET LOCAL search_path TO "{schema}"')
        sql = (Path(__file__).parents[1] / "migrations/073_customer_guard.sql").read_text()
        cur.execute(sql.replace("BEGIN;", "").replace("COMMIT;", ""))
        cur.execute((Path(__file__).parents[1] / "migrations/008_ozon_posting.sql").read_text())
        cur.execute((Path(__file__).parents[1] / "migrations/074_ozon_expense_basis.sql").read_text())
        cur.execute((Path(__file__).parents[1] / "migrations/075_customer_guard_runtime.sql").read_text()
                    .replace("BEGIN;","").replace("COMMIT;",""))

    @contextmanager
    def isolated():
        with original() as conn:
            with conn.cursor() as cur:
                cur.execute(f'SET LOCAL search_path TO "{schema}"')
            yield conn
    monkeypatch.setattr(db, "get_conn", isolated)
    try:
        yield
    finally:
        with original() as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


def ingest(posts, at=NOW):
    store.ingest(posts, at, at, NOW-timedelta(days=1), at, mirror_raw=True)


def test_common_expense_floor_and_positive_remainder_below_target():
    from core.ozon_expense_basis import floor_from_keep
    post = posting()
    post["products"][0]["price"] = "728"
    keep = Decimal("0.39207839417185169693031269529450722916")
    r = dict(rule(), retention=1-keep, version="september",
             base_from="2026-09-01", base_to="2026-09-30")
    value = domain.economics(post, Decimal("69.28"), "actual", r, NOW, True)
    assert Decimal(value["floor"]) == floor_from_keep("69.28", 1, keep) == Decimal("941.85")
    assert value["gap"] == "-213.85"
    assert value["estimated_net"] == "216.15"
    assert value["signal"] == "below_floor"


def test_basis_reprice_preserves_source_time_history_and_is_idempotent(pg):
    ingest([posting()])
    checked = NOW+timedelta(minutes=10)
    job = store.claim(checked)
    old = domain.economics(posting(), Decimal("510.08"), "fifo_estimate", rule(), checked, True)
    assert store.finish(job, old, checked)
    before = db.query("SELECT * FROM customer_guard_checks")
    snapshots = db.query("SELECT count(*) n FROM customer_guard_snapshots")[0]["n"]
    r = dict(rule(), retention=Decimal(".5"), version="new")
    assert store.reprice(r, checked+timedelta(minutes=3)) == 1
    assert store.reprice(r, checked+timedelta(minutes=4)) == 0
    rows = db.query("SELECT * FROM customer_guard_checks ORDER BY id")
    assert rows[0] == before[0]
    assert rows[1]["result"]["floor"] == "1620.16"
    assert rows[1]["result"]["source_check_id"] == rows[0]["id"]
    assert rows[1]["result"]["cost_observed_at"] == checked.isoformat()
    assert db.query("SELECT ms_checked_at FROM customer_guard_orders")[0]["ms_checked_at"] == checked
    assert db.query("SELECT count(*) n FROM customer_guard_snapshots")[0]["n"] == snapshots
    # A changed marketplace revision must be checked against MS before local repricing.
    changed = posting()
    changed["products"][0]["quantity"] = 2
    ingest([changed], checked+timedelta(minutes=5))
    assert store.reprice(dict(r, version="newer"), checked+timedelta(minutes=6)) == 0


def test_tc_structured_component_codes_and_cached_recheck(pg):
    from shipment_guard.bundles import component_counts
    from shipment_guard.ms import SourceError
    record = {"external_code":"3109","incoming_references":[],"outgoing_references":[]}
    assert component_counts([str(record), record, "3110"]) == {"3109":2, "3110":1}
    with pytest.raises(SourceError):
        component_counts(["{'external_code': '3109', 'quantity': 8}"])
    with pytest.raises(SourceError):
        component_counts(["{invalid_record}"])
    ingest([posting()])
    checked = NOW+timedelta(minutes=10)
    job = store.claim(checked)
    old = domain.economics(posting(), Decimal("510.08"), "fifo_estimate", rule(), checked)
    # Saved public positions use external_code, as in evaluate().
    old["positions"] = [{"quantity":1,"external_code":"3109"}]
    assert store.finish(job, old, checked)
    resolver = lambda code: (component_counts([str(record)]) if code=="set" else {code:1}, {})
    assert store.reprice(rule(), checked+timedelta(minutes=1), resolver) == 1
    result = db.query("SELECT result FROM customer_guard_checks ORDER BY id DESC LIMIT 1")[0]["result"]
    assert result["signal"] == "ok"
    assert result["composition"]["verified"]
    assert store.reprice(rule(), checked+timedelta(minutes=2), resolver) == 0

def test_first_seen_delay_duplicates_and_late_answers(pg):
    ingest([posting()])
    ingest([posting()], NOW+timedelta(minutes=8))
    row = db.query("SELECT * FROM customer_guard_orders")[0]
    assert row["first_seen_at"] == NOW
    assert row["ms_due_at"] == NOW+timedelta(minutes=10)
    assert row["generation"] == 1
    assert store.claim(NOW+timedelta(minutes=9, seconds=59)) is None
    stale = posting(status="cancelled")
    ingest([stale], NOW-timedelta(seconds=1))
    assert db.query("SELECT mp_status FROM customer_guard_orders")[0]["mp_status"] == "awaiting_packaging"
    assert store.claim(NOW+timedelta(minutes=10)) is not None
    assert db.query("SELECT count(*) n FROM customer_guard_orders")[0]["n"] == 1


def test_failure_not_absence_and_retry_is_delayed(pg):
    ingest([posting()])
    job = store.claim(NOW+timedelta(minutes=10))
    store.fail(job, "ms_429", NOW+timedelta(minutes=10), 20)
    assert not db.query("SELECT * FROM customer_guard_incidents")
    assert store.claim(NOW+timedelta(minutes=19)) is None
    assert store.claim(NOW+timedelta(minutes=20)) is not None


def test_cancel_invalidates_lease_and_prevents_missing_signal(pg):
    ingest([posting()])
    job = store.claim(NOW+timedelta(minutes=10))
    assert not store.refresh(job, posting(status="cancelled"), NOW+timedelta(minutes=11))
    assert not store.finish(job, {"signal":"missing_ms"}, NOW+timedelta(minutes=11))
    assert not db.query("SELECT * FROM customer_guard_incidents")


def test_stale_lease_and_changed_composition_cannot_finish(pg):
    ingest([posting()])
    job = store.claim(NOW+timedelta(minutes=10))
    modified = posting()
    modified["products"][0]["quantity"] = 2
    assert not store.refresh(job, modified, NOW+timedelta(minutes=11))
    assert not store.finish(job, {"signal":"missing_ms"}, NOW+timedelta(minutes=11))
    next_job = store.claim(NOW+timedelta(minutes=11))
    assert next_job["generation"] == 2
    assert not store.finish(next_job, {"signal":"missing_ms"}, NOW+timedelta(minutes=22))


def test_loss_incident_dedup_and_resolve_on_draft(pg):
    ingest([posting()])
    job = store.claim(NOW+timedelta(minutes=10))
    loss = dict(signal="below_floor", estimated_net="-1", floor="100", rule_version="test")
    ready = {"id":str(uuid4()), "state":{"name":"Подтвержден"}}
    assert store.finish(job, loss, NOW+timedelta(minutes=11), ready)
    assert not store.finish(job, loss, NOW+timedelta(minutes=11), ready)
    assert len(db.query("SELECT * FROM customer_guard_incidents")) == 1
    changed = posting(status="awaiting_deliver")
    ingest([changed], NOW+timedelta(minutes=12))
    job2 = store.claim(NOW+timedelta(minutes=12))
    document = {"id":str(uuid4()), "state":{"name":"Ожидает подтверждения"}}
    assert store.finish(job2, {"signal":"draft"}, NOW+timedelta(minutes=13), document)
    incident = db.query("SELECT * FROM customer_guard_incidents")[0]
    assert incident["resolved_at"] is not None
    assert incident["delivery"] == "suppressed"


def test_partial_window_rolls_back_raw_jobs_and_checkpoint(pg):
    invalid = posting("123-002-1")
    invalid["products"][0]["price"] = {"amount":"1", "currency":"USD"}
    with pytest.raises(ValueError):
        ingest([posting(), invalid])
    for table in ["customer_guard_orders", "customer_guard_jobs", "raw_ozon_posting",
                  "customer_guard_sync_state"]:
        assert not db.query("SELECT * FROM "+table)


def test_pilot_does_not_change_shared_raw_before_financial_migration(pg):
    store.ingest([posting()], NOW, NOW, NOW-timedelta(days=1), NOW)
    assert not db.query("SELECT * FROM raw_ozon_posting")
    assert len(db.query("SELECT * FROM customer_guard_orders")) == 1


def test_split_postings_have_separate_orders_and_jobs(pg):
    ingest([posting("123-001-1"), posting("123-001-2")])
    assert len(db.query("SELECT * FROM customer_guard_orders")) == 2
    assert len(db.query("SELECT * FROM customer_guard_jobs")) == 2
    assert store.claim(NOW+timedelta(minutes=9), "123-001-2") is None
    job = store.claim(NOW+timedelta(minutes=10), "123-001-2")
    assert job["order"]["posting_number"] == "123-001-2"
    assert store.claim(NOW+timedelta(minutes=10), "123-001-3") is None


def test_unknown_mp_status_stops_pending_work_without_missing_alert(pg):
    ingest([posting()])
    job = store.claim(NOW+timedelta(minutes=10))
    assert not store.refresh(job, posting(status="new_unknown_status"), NOW+timedelta(minutes=11))
    assert store.claim(NOW+timedelta(minutes=22)) is None
    assert not db.query("SELECT * FROM customer_guard_incidents")


def test_dashboard_read_only_data_filters_and_history(pg):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.customer_guard_api import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ingest([posting(), posting("123-001-2")])
    response = client.get("/api/customer-orders")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["rows"][0]["mp_price"] == "1000.00"
    assert data["queue"]["pending"] == 2
    assert client.get("/api/customer-orders", params={"search":"-2"}).json()["total"] == 1
    assert client.get("/api/customer-orders", params={"signal":"missing_ms"}).json()["total"] == 0
    assert client.get("/api/customer-orders", params={"limit":201}).status_code == 422
    assert client.get("/warehouse/customer-orders").status_code == 200
    job = store.claim(NOW+timedelta(minutes=10))
    store.finish(job, {"signal":"missing_ms"}, NOW+timedelta(minutes=11))
    filtered = client.get("/api/customer-orders", params={"signal":"missing_ms"}).json()
    assert filtered["total"] == 1
    assert len(client.get(f"/api/customer-orders/{job['order_id']}/history").json()["rows"]) == 1


@pytest.mark.parametrize('net,version,current,error,expected',[
    ('-100.60','current',True,None,1),('0.00','current',True,None,1),
    ('216.15','current',True,None,0),(None,'current',True,None,0),
    ('NaN','current',True,None,0),('broken','current',True,None,0),
    ('-1','old',True,None,0),('-1','current',False,None,0),
    ('-1','current',True,'ms_429',0),
])
def test_profit_filter_excludes_unknown_and_stale_results(pg,monkeypatch,net,version,current,error,expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web import customer_guard_api as api
    monkeypatch.setattr(api,'get_basis',lambda account:{'version':'current'})
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    ingest([posting()]);job=store.claim(NOW+timedelta(minutes=10))
    assert store.finish(job,dict(signal='below_floor',estimated_net=net,rule_version=version,floor='100'),
                        NOW+timedelta(minutes=11))
    if not current:db.execute('UPDATE customer_guard_orders SET generation=generation+1')
    if error:db.execute('UPDATE customer_guard_jobs SET error=%s',(error,))
    data=client.get('/api/customer-orders',params={'signal':'non_positive_profit'}).json()
    assert data['total']==expected and len(data['rows'])==expected
    def missing_basis(account):
        raise api.BasisUnavailable('No current basis')
    monkeypatch.setattr(api,'get_basis',missing_basis)
    assert client.get('/api/customer-orders',params={'signal':'non_positive_profit'}).json()['total']==0


def test_dashboard_sorts_by_market_time_not_import_time(pg):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.customer_guard_api import router
    app=FastAPI();app.include_router(router);client=TestClient(app)
    older=posting('older-1');newer=posting('newer-1');unknown=posting('unknown-1')
    older['in_process_at']=(NOW-timedelta(days=2)).isoformat()
    newer['in_process_at']=(NOW-timedelta(days=1)).isoformat()
    unknown.pop('in_process_at')
    ingest([newer,older,unknown])
    rows=client.get('/api/customer-orders').json()['rows']
    assert [r['posting_number'] for r in rows]==['unknown-1','newer-1','older-1']
    assert rows[0]['mp_received_at'] is None
    assert client.get('/api/customer-orders',params={'offset':1,'limit':1}).json()['rows'][0]['posting_number']=='newer-1'
