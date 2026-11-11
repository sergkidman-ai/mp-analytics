# поток: fin
"""Recompute only open Market months from existing order, service and FIFO rows."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
from psycopg2.extensions import parse_dsn,make_dsn
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db
ROOT=Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))


def main():
    params=parse_dsn(db.DATABASE_URL);params['dbname']='bi_history_20261104'
    db.DATABASE_URL=make_dsn(**params);os.environ['DATABASE_URL']=db.DATABASE_URL
    assert db.query('SELECT current_database() name')[0]['name']==params['dbname']
    day=dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()))
    since=(day.replace(day=1)-dt.timedelta(days=1)).replace(day=1)
    closed=set(json.loads(Path('/opt/mp-analytics/reports/data/mp_yandex_hist.json').read_text()).get('period_keys',[]))
    while since.strftime('%Y-%m') in closed:since=(since+dt.timedelta(days=32)).replace(day=1)
    if since>day:raise RuntimeError('No open Market month')
    unknown=db.query("SELECT count(*) n FROM ya_cogs_demand WHERE ym>=%s AND status='done' AND (cogs<=0 OR method NOT IN ('ms_fifo','manual'))",(since,))[0]['n']
    if unknown:raise RuntimeError('Open Market shipment FIFO is incomplete')
    coverage=db.query("""WITH o AS(SELECT DISTINCT ON(order_id) order_id,payload FROM raw_yandex_stats_order
       WHERE account='ya_acc1' ORDER BY order_id,loaded_at DESC)
       SELECT count(*) delivered,count(*)FILTER(WHERE d.demand_name IS NULL) missing
       FROM o LEFT JOIN ya_cogs_demand d ON d.account='ya_acc1' AND d.demand_name=o.order_id
       WHERE o.payload->>'status'='DELIVERED' AND o.payload->>'creationDate'>=%s""",(since.isoformat(),))[0]
    if coverage['missing']:raise RuntimeError('Delivered Market orders have no confirmed FIFO')
    from collectors import yandex_monthly as ya
    ya._cost_map=lambda:{}
    records=ya.recompute(since.isoformat(),until=day.isoformat())
    if any(r['month'][:7]in closed for r in records):raise RuntimeError('Closed Market month was recomputed')
    result={'status':'done','since':since.isoformat(),'months':len(records),'delivered_coverage':coverage,'updated_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    (ROOT/'build_market.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
