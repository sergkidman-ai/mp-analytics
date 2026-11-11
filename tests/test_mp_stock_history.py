# поток: fin
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from core import db
from collectors.mp_stock_history import backfill, record_snapshot


def test_refresh_continues_after_one_source_failure():
    from ops.mp_inventory_refresh import refresh
    called = []
    def fail():
        raise RuntimeError('source offline')
    result = refresh([('failed', fail), ('next', lambda: called.append(True))])
    assert result['failed']['status'] == 'error'
    assert result['next']['status'] == 'ok'
    assert called == [True]


def test_restored_baseline_is_immutable_even_for_empty_account(stock_schema):
    from ops.mp_inventory_refresh import freeze_baseline
    db.execute((Path(__file__).parents[1] / 'migrations/081_mp_inventory_baseline.sql').read_text())
    day = '2026-11-05'
    record_snapshot('ozon', 'oz_acc1', day, [dict(sku='1', warehouse='A', free_to_sell=3)])
    record_snapshot('ozon', 'oz_acc2', day, [])
    freeze_baseline()
    record_snapshot('ozon', 'oz_acc1', day, [dict(sku='1', warehouse='A', free_to_sell=8)])
    record_snapshot('ozon', 'oz_acc2', day, [dict(sku='2', warehouse='B', free_to_sell=1)])
    freeze_baseline()
    rows = db.query('SELECT account,usable_available FROM mp_inventory_baseline_item')
    assert len(rows) == 1 and rows[0]['account'] == 'oz_acc1' and rows[0]['usable_available'] == 3
    assert db.query("SELECT usable_available FROM mp_inventory_baseline_capture WHERE account='oz_acc2'")[0]['usable_available'] == 0


def test_first_observation_has_no_invented_stock_increase(stock_schema):
    from web.mp_inventory_api import stock_data
    db.execute((Path(__file__).parents[1] / 'migrations/081_mp_inventory_baseline.sql').read_text())
    record_snapshot('ozon', 'oz_acc1', '2026-11-05', [dict(sku='1', warehouse='A', free_to_sell=3)])
    data = stock_data(on='2026-11-05')
    assert data['rows'][0]['current'] == '3'
    assert data['rows'][0]['change'] is None
    assert data['summary'][0]['change'] is None


@pytest.fixture
def stock_schema(monkeypatch):
    schema = "stock_history_test_" + uuid4().hex
    original = db.get_conn
    root = Path(__file__).parents[1]
    with original() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET LOCAL search_path TO "{schema}"')
        for name in ("003_wb_stocks.sql", "015_ozon_fbo_stock.sql",
                     "512_wb_loss_official.sql", "078_mp_stock_history.sql",
                     "081_mp_inventory_baseline.sql", "096_wb_damage_stock_evidence.sql"):
            cur.execute((root / "migrations" / name).read_text())

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


def test_stock_history_excludes_official_loss_and_preserves_raw(stock_schema):
    db.execute("""INSERT INTO wb_loss_document
      (id,account,warehouse,source_archive,source_member,pdf_name,header_date,pdf,signature_present)
      VALUES ('act','wb_acc1','Склад WB РФ','a','b','c','2026-08-28','\\x00',true)""")
    db.execute("""INSERT INTO wb_loss_item(document_id,nm_id,vendor_code,qty,source_pages)
      VALUES ('act',123,'sku',2,ARRAY[1])""")
    db.execute("""INSERT INTO wb_stocks
      (account,nm_id,warehouse,quantity,quantity_full,captured_at)
      VALUES ('wb_acc1',123,'Склад WB РФ',3,3,'2026-10-28')""")
    assert record_snapshot("wb", "wb_acc1", "2026-10-28", [
        dict(nm_id=123,warehouse="Склад WB РФ",quantity=3,quantity_full=3)]) == 1
    row = db.query("SELECT raw_available,excluded_loss,usable_available FROM mp_stock_history")[0]
    assert tuple(row.values()) == (3, 2, 1)
    assert db.query("SELECT quantity FROM wb_stocks")[0]["quantity"] == 3
    assert db.query("SELECT usable_quantity FROM wb_stocks_usable")[0]["usable_quantity"] == 1
    db.execute((Path(__file__).parents[1] / "migrations/081_mp_inventory_baseline.sql").read_text())
    assert db.query("SELECT usable_available FROM mp_inventory_baseline_item")[0]["usable_available"] == 1
    assert backfill()["wb_rows"] == 0
    record_snapshot("wb", "wb_acc1", "2026-10-28", [])
    assert db.query("SELECT source_rows,usable_available FROM mp_stock_capture")[0] == {
        "source_rows": 0, "usable_available": 0}
    assert db.query("SELECT count(*) n FROM mp_stock_history")[0]["n"] == 0
    assert db.query("SELECT usable_available FROM mp_inventory_baseline_item")[0]["usable_available"] == 1


def test_damaged_stock_stays_evidence_after_refresh_and_baseline(stock_schema):
    from ops.mp_inventory_refresh import freeze_baseline
    from web.mp_inventory_api import stock_data
    raw = [dict(nm_id=123, warehouse="Коледино", quantity=8, quantity_full=8),
           dict(nm_id=123, warehouse="Склад WB РФ", quantity=1, quantity_full=1)]
    record_snapshot("wb", "wb_acc1", "2026-11-05", raw)
    freeze_baseline()
    record_snapshot("wb", "wb_acc1", "2026-11-06", [dict(x, quantity=12, quantity_full=12)
                    if x["warehouse"] == "Коледино" else x for x in raw])
    data = stock_data(on="2026-11-06")
    assert data["summary"][0]["available"] == "1.0"
    assert data["summary"][0]["change"] == "0.0"
    assert {r["warehouse"] for r in data["rows"]} == {"Склад WB РФ"}
    assert "Коледино" not in {r["name"] for r in data["warehouses"]}
    assert db.query("SELECT raw_available,excluded_loss,usable_available FROM mp_stock_history WHERE warehouse='Коледино' AND captured_at='2026-11-06'")[0] == dict(raw_available=12,excluded_loss=12,usable_available=0)
    assert db.query("SELECT raw_available,usable_available FROM mp_inventory_baseline_item WHERE warehouse='Коледино'")[0] == dict(raw_available=8,usable_available=0)


def test_damage_reclassification_repairs_existing_history_without_changing_raw(stock_schema):
    from ops.mp_inventory_refresh import freeze_baseline
    root = Path(__file__).parents[1]
    db.execute("DELETE FROM wb_damage_stock_warehouse WHERE warehouse='Коледино'")
    record_snapshot("wb", "wb_acc1", "2026-11-05", [dict(nm_id=1,warehouse="Коледино",quantity=7,quantity_full=7)])
    freeze_baseline()
    before = db.query("SELECT raw_available,recorded_at FROM mp_stock_history")[0]
    for _ in range(2):
        db.execute((root / "migrations/096_wb_damage_stock_evidence.sql").read_text())
    after = db.query("SELECT raw_available,recorded_at,usable_available FROM mp_stock_history")[0]
    assert after["raw_available"] == before["raw_available"] == 7
    assert after["recorded_at"] == before["recorded_at"]
    assert after["usable_available"] == 0
    assert db.query("SELECT usable_available,raw_available FROM mp_stock_capture")[0] == dict(usable_available=0,raw_available=7)
    assert db.query("SELECT usable_available,raw_available FROM mp_inventory_baseline_item")[0] == dict(usable_available=0,raw_available=7)
