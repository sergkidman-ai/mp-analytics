# поток: fin
"""An empty stock snapshot must not hide verified historical shipment FIFO."""
import datetime as dt
from unittest import TestCase
from unittest.mock import patch
from core import db
from reports import fifo_fallback
import psycopg2
from psycopg2.extras import RealDictCursor


class CatalogRecoveryTests(TestCase):
    def setUp(self):
        self.conn=psycopg2.connect(db.DATABASE_URL)
        self.addCleanup(self.conn.close)
        with self.conn.cursor() as q:
            for ddl in (
                'CREATE TEMP TABLE products(ms_id text, external_code text)',
                'CREATE TEMP TABLE ms_product(ms_id text, external_code text, buy_price numeric)',
                'CREATE TEMP TABLE ms_demand_cogs(demand_id text,moment timestamp,demand_name text,org text,agent text,cogs numeric,qty numeric)',
                'CREATE TEMP TABLE ms_demand_pos(demand_id text, ms_id text, qty numeric, cost numeric)',
                'CREATE TEMP TABLE set_cost(external_code text, components jsonb,n_components int)',
                'CREATE TEMP TABLE prc_tc_link(external_code text,ref_code text)',
            ):q.execute(ddl)
            q.execute("INSERT INTO ms_product VALUES('p','0949',999999)")
            q.execute("INSERT INTO ms_demand_cogs(demand_id,moment) VALUES('d','2026-10-01')")
            q.execute("INSERT INTO ms_demand_pos VALUES('d','p',2,500)")
        def query(statement,params=None):
            with self.conn.cursor(cursor_factory=RealDictCursor) as q:
                q.execute(statement,params);return list(q.fetchall())
        self.patcher=patch.object(fifo_fallback.db,'query',side_effect=query)
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def test_zero_current_stock_still_uses_historical_fifo(self):
        ff=fifo_fallback.load()
        self.assertEqual(ff.unit('0949',dt.date(2026,10,25)),(250.0,'tovar_fifo'))

    def test_catalog_price_and_future_fifo_cannot_supply_past_cost(self):
        ff=fifo_fallback.load()
        self.assertEqual(ff.unit('0949',dt.date(2026,9,30)),(None,None))
        with self.conn.cursor() as q:q.execute('DELETE FROM ms_demand_pos')
        self.assertEqual(fifo_fallback.load().unit('0949',dt.date(2026,10,25)),(None,None))

    def test_partial_or_missing_positions_do_not_prove_whole_wb_demand(self):
        from reports import margin_by_sku as wb
        with self.conn.cursor() as q:
            q.execute("UPDATE ms_demand_cogs SET demand_name='order',org='org',agent='Покупатель ВБ',cogs=500,qty=2")
        self.assertEqual(wb.demand_cogs_from_cache('org',strict=True),{'order':(500.0,2.0)})
        with self.conn.cursor() as q:q.execute("UPDATE ms_demand_pos SET qty=1")
        self.assertEqual(wb.demand_cogs_from_cache('org',strict=True),{})
        with self.conn.cursor() as q:q.execute("UPDATE ms_demand_pos SET qty=2,cost=0")
        self.assertEqual(wb.demand_cogs_from_cache('org',strict=True),{})

    def test_one_bad_ozon_fragment_invalidates_whole_posting_cost(self):
        from reports import margin_ozon_sku as oz
        with self.conn.cursor() as q:
            q.execute("UPDATE ms_demand_cogs SET demand_name='1000-001-1',org='org',agent='Покупатель Озон',cogs=500,qty=2")
            q.execute("INSERT INTO ms_demand_cogs VALUES('partial','2026-10-01','1000-001-2','org','Покупатель Озон',100,1)")
            q.execute("INSERT INTO ms_demand_pos VALUES('partial','p',1,0)")
        self.assertNotIn('1000-001',oz.posting_cogs_map('org',strict=True))
        self.assertEqual(oz.posting_cogs_map('org')['1000-001'],600)
