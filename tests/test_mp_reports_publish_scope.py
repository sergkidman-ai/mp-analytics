# поток: fin
"""Publication must merge natural identities and preserve closed months."""
from unittest import TestCase
import psycopg2
from core import db
from tools.mp_reports_publish import upsert_sql


class PublishScopeTests(TestCase):
    def setUp(self):
        self.conn=psycopg2.connect(db.DATABASE_URL);self.addCleanup(self.conn.close)

    def test_natural_key_merge_preserves_live_surrogate_ids(self):
        with self.conn.cursor() as q:
            q.execute('CREATE TEMP TABLE financial_raw(id serial primary key,account text,operation text,amount numeric,unique(account,operation))')
            q.execute("INSERT INTO financial_raw(account,operation,amount) VALUES('a','old',10),('a','changed',20)")
            q.execute('CREATE TEMP TABLE incoming(id int,account text,operation text,amount numeric)')
            q.execute("INSERT INTO incoming VALUES(1,'a','changed',25),(2,'a','new',30)")
            q.execute(upsert_sql('financial_raw','incoming',['account','operation','amount'],['account','operation']))
            q.execute('SELECT id,operation,amount::int FROM financial_raw ORDER BY id')
            rows=q.fetchall()
            self.assertEqual(rows[:2],[(1,'old',10),(2,'changed',25)])
            self.assertEqual(rows[2][1:],('new',30))
            self.assertNotIn(rows[2][0],(1,2))

    def test_scoped_merge_does_not_change_closed_month(self):
        with self.conn.cursor() as q:
            q.execute('CREATE TEMP TABLE monthly(month date primary key,cost numeric)')
            q.execute("INSERT INTO monthly VALUES('2026-09-01',467045),('2026-10-01',0)")
            q.execute('CREATE TEMP TABLE incoming(month date,cost numeric)')
            q.execute("INSERT INTO incoming VALUES('2026-09-01',999999),('2026-10-01',384740)")
            q.execute(upsert_sql('monthly','incoming',['month','cost'],['month'],"month>='2026-10-01'"))
            q.execute('SELECT cost::int FROM monthly ORDER BY month')
            self.assertEqual(q.fetchall(),[(467045,),(384740,)])

    def test_market_manual_cost_survives_automatic_refresh(self):
        with self.conn.cursor() as q:
            q.execute('CREATE TEMP TABLE ya_cogs_demand(account text,demand_name text,cogs numeric,method text,status text,primary key(account,demand_name))')
            q.execute("INSERT INTO ya_cogs_demand VALUES('ya_acc1','o',123,'manual','done')")
            q.execute('CREATE TEMP TABLE incoming(account text,demand_name text,cogs numeric,method text,status text)')
            q.execute("INSERT INTO incoming VALUES('ya_acc1','o',321,'ms_fifo','return_stock')")
            q.execute(upsert_sql('ya_cogs_demand','incoming',['account','demand_name','cogs','method','status'],['account','demand_name']))
            q.execute('SELECT cogs::int,method,status FROM ya_cogs_demand')
            self.assertEqual(q.fetchone(),(123,'manual','return_stock'))
