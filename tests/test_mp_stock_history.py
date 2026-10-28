# поток: fin
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from core import db
from collectors.mp_stock_history import backfill, record_snapshot


@pytest.fixture
def stock_schema(monkeypatch):
    schema = "stock_history_test_" + uuid4().hex
    original = db.get_conn
    root = Path(__file__).parents[1]
    with original() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET LOCAL search_path TO "{schema}"')
        for name in ("003_wb_stocks.sql", "015_ozon_fbo_stock.sql",
                     "512_wb_loss_official.sql", "078_mp_stock_history.sql"):
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
      VALUES ('act','wb_acc1','Коледино','a','b','c','2026-08-28','\\x00',true)""")
    db.execute("""INSERT INTO wb_loss_item(document_id,nm_id,vendor_code,qty,source_pages)
      VALUES ('act',123,'sku',2,ARRAY[1])""")
    db.execute("""INSERT INTO wb_stocks
      (account,nm_id,warehouse,quantity,quantity_full,captured_at)
      VALUES ('wb_acc1',123,'Коледино',3,3,'2026-10-28')""")
    assert record_snapshot("wb", "wb_acc1", "2026-10-28", [
        dict(nm_id=123,warehouse="Коледино",quantity=3,quantity_full=3)]) == 1
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
