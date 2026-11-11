# поток: cf
import datetime as dt
from pathlib import Path

from psycopg2.extras import RealDictCursor

from core import db
from cf import mp_forecast


def test_schedule_storage_is_idempotent_and_filters_dates_on_postgres(monkeypatch):
    sql = (Path(__file__).resolve().parents[1] /
           'migrations/807_cf_ozon_payout_schedule.sql').read_text()
    # Production tables are never touched: the same schema lives in this connection's pg_temp.
    sql = sql.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TEMP TABLE IF NOT EXISTS')
    with db.get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        cur.execute("""insert into cf_ozon_payout_schedule
            (account,payment_date,amount,components,source) values
            ('oz_acc1','2026-11-04',50,'[50]','test'),
            ('oz_acc1','2026-11-11',100,'[100]','test'),
            ('oz_acc1','2026-11-25',150,'[110,40]','test'),
            ('oz_acc2','2026-11-25',25,'[25]','test'),
            ('oz_acc2','2026-12-02',30,'[30]','test')""")
        cur.execute(sql)

        def query(query_sql, params=None):
            cur.execute(query_sql, params)
            return [dict(row) for row in cur.fetchall()]

        monkeypatch.setattr(db, 'query', query)
        rows = mp_forecast._ozon_lk_schedule(dt.date(2026, 11, 10), dt.date(2026, 11, 25))
        assert [(r['account'], r['payment_date'], r['amount']) for r in rows] == [
            ('oz_acc1', dt.date(2026, 11, 11), 100),
            ('oz_acc1', dt.date(2026, 11, 25), 150),
            ('oz_acc2', dt.date(2026, 11, 25), 25),
        ]
        assert rows[1]['components'] == [110, 40]
