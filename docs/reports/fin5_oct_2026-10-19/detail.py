import os
os.environ['PGOPTIONS']='-c default_transaction_read_only=on'
import sys,json
sys.path.insert(0,'/opt/mp-analytics')
from pathlib import Path
from core import db
from reports import ozon_mp_report as O,yandex_mp_report as Y
from collectors import yandex_finance_basis as B
P=Path('/opt/mp-analytics/docs/reports/fin5_oct_2026-10-19')
def save(n,x):(P/n).write_text(json.dumps(x,ensure_ascii=False,default=str,indent=2))
q="""SELECT account,to_char((payload->>'operation_date')::date,'YYYY-MM') AS period,payload->>'operation_type' code,max(payload->>'operation_type_name') name,count(*) records,sum((payload->>'amount')::numeric) amount,sum((payload->>'accruals_for_sale')::numeric) sales,sum((payload->>'sale_commission')::numeric) commission,sum((payload->>'amount')::numeric-(payload->>'accruals_for_sale')::numeric-(payload->>'sale_commission')::numeric-coalesce((SELECT sum((s->>'price')::numeric) FROM jsonb_array_elements(payload->'services') s),0)) residual FROM raw_ozon_transaction WHERE (payload->>'operation_date')::date>='2026-08-01' AND (payload->>'operation_date')::date<'2026-11-01' GROUP BY 1,2,3 ORDER BY 1,2,3"""
operations=db.query(q);save('ozon_operation_types.json',operations)
print('OZON UNCLASSIFIED')
for r in operations:
 if r['period']=='2026-10' and O._resid_line(r['code'])=='unclassified' and abs(r['residual'] or 0)>.01: print(r)
q="SELECT order_id,payload->'payments' payments,payload->>'creationDate' created,payload->>'status' status,loaded_at FROM raw_yandex_stats_order WHERE account='ya_acc1' AND EXISTS (SELECT 1 FROM jsonb_array_elements(payload->'payments') p WHERE left(p->>'date',7)>='2026-09')"
orders=db.query(q);save('yandex_payment_source_coverage.json',{'orders':len(orders),'min_created':min(x['created'] for x in orders),'max_created':max(x['created'] for x in orders),'min_loaded':min(x['loaded_at'] for x in orders),'max_loaded':max(x['loaded_at'] for x in orders)})
byid={str(x['order_id']):{'payments':x['payments']} for x in orders};creation={str(x['order_id']):x['created'][:7] for x in orders}
payments={month:B.payments(byid,month) for month in ['2026-09','2026-10']};save('yandex_calendar_payments.json',payments)
summary=[]
for month,rows in payments.items():
 summary.append(dict(month=month,receipts=sum(r['amount'] for r in rows if r['type']=='PAYMENT'),refunds=sum(r['amount'] for r in rows if r['type']=='REFUND'),previous_order_receipts=sum(r['amount'] for r in rows if r['type']=='PAYMENT' and creation[r['order_id']]<month),payment_rows=len(rows),latest_payment=max(r['date'] for r in rows),created_month_totals={m:sum(r['amount'] for r in rows if r['type']=='PAYMENT' and creation[r['order_id']]==m) for m in sorted(set(creation[r['order_id']] for r in rows))}))
save('yandex_calendar_comparison.json',summary)
print('YANDEX CALENDAR PAYMENTS')
for r in summary: print(r)
save('yandex_service_breakdown.json',db.query("SELECT ym,category,service,count(*) records,min(svc_date) first_date,max(svc_date) last_date,sum(cost) cash,sum(netting) netting,sum(cost+coalesce(netting,0)) accrued FROM raw_yandex_services WHERE ym>='2026-08' GROUP BY 1,2,3 ORDER BY 1,2,3"))
save('ozon_daily.json',db.query("""SELECT account,(payload->>'operation_date')::date AS day,count(*) records,sum(greatest((payload->>'accruals_for_sale')::numeric,0)) sales,sum(-least((payload->>'accruals_for_sale')::numeric,0)) refunds,sum(CASE WHEN (payload->>'accruals_for_sale')::numeric>0 THEN -(payload->>'sale_commission')::numeric ELSE 0 END) sold_commission,sum(CASE WHEN (payload->>'accruals_for_sale')::numeric<=0 THEN -(payload->>'sale_commission')::numeric ELSE 0 END) other_commission FROM raw_ozon_transaction WHERE (payload->>'operation_date')::date>='2026-08-01' GROUP BY 1,2 ORDER BY 1,2"""))
save('ozon_big_refunds.json',db.query("""SELECT account,payload->>'operation_date' AS day,payload->>'operation_type' code,payload->>'operation_type_name' name,(payload->>'accruals_for_sale')::numeric sale_reversal,(payload->>'sale_commission')::numeric commission_return,payload->'posting' posting,payload->'items' items FROM raw_ozon_transaction WHERE (payload->>'operation_date')::date>='2026-10-01' AND (payload->>'accruals_for_sale')::numeric<0 ORDER BY (payload->>'accruals_for_sale')::numeric LIMIT 30"""))
print('Detail data saved.')
