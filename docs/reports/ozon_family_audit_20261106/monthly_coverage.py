# поток: ozon-family; bounded read-only aggregates, no financial analysis.
import sys,pathlib,json,datetime
R=pathlib.Path('/opt/mp-analytics');sys.path.insert(0,str(R))
from core import db
import psycopg2,psycopg2.extras
O=pathlib.Path(__file__).parent/'snapshot'
qs={
'realization':"SELECT year,month,count(*) rows,min(loaded_at) first_loaded,max(loaded_at) last_loaded FROM raw_ozon_realization WHERE account='oz_acc1' GROUP BY year,month ORDER BY year,month",
'margin':"SELECT period_from,period_to,count(*) rows,count(distinct article) articles,count(*) FILTER(WHERE cogs IS NULL) null_cogs,min(computed_at) first_computed,max(computed_at) last_computed FROM margin_by_sku WHERE account='oz_acc1' AND platform='ozon' GROUP BY period_from,period_to ORDER BY period_from,period_to",
'transaction':"SELECT date_trunc('month',period_from)::date AS period_month,count(*) rows,min(period_to) first_period_to,max(period_to) last_period_to,min(loaded_at) first_loaded,max(loaded_at) last_loaded,min(payload->>'operation_date') first_operation,max(payload->>'operation_date') last_operation,count(distinct left(payload->>'operation_date',10)) operation_days FROM raw_ozon_transaction WHERE account='oz_acc1' GROUP BY 1 ORDER BY 1",
'accrual':"SELECT date_trunc('month',accrual_date)::date AS period_month,count(*) rows,count(distinct accrual_date) days,min(accrual_date) first_day,max(accrual_date) last_day FROM raw_ozon_accrual WHERE account='oz_acc1' GROUP BY 1 ORDER BY 1"}
c=psycopg2.connect(db.DATABASE_URL);c.set_session(readonly=True,isolation_level='REPEATABLE READ')
out={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'queries':{}}
with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
 for k,q in qs.items():
  cur.execute(q);rows=[dict(r) for r in cur.fetchall()];out['queries'][k]={'sql':q,'rows':rows};print(k,len(rows))
c.rollback();c.close()
(O/'db_year_monthly_coverage.json').write_text(json.dumps(out,ensure_ascii=False,default=str))
