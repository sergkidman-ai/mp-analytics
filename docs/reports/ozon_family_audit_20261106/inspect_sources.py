# поток: ozon-family; read-only schema inventory, no business mutations.
import sys, json, pathlib, datetime
ROOT = pathlib.Path('/opt/mp-analytics')
sys.path.insert(0, str(ROOT))
from core import db
import psycopg2, psycopg2.extras
OUT = pathlib.Path(__file__).parent / 'snapshot'
conn = psycopg2.connect(db.DATABASE_URL)
conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
    c.execute("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position")
    rows = [dict(r) for r in c.fetchall()]
(OUT/'db_schema.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
tables = sorted({r['table_name'] for r in rows if any(s in r['table_name'] for s in ['ozon','ms_product','bundle','compat','supplier_stock','margin','tk_'])})
for t in tables:
    print(t + ': ' + ', '.join(r['column_name'] for r in rows if r['table_name']==t))
conn.rollback(); conn.close()
