# поток: ozon-family. Read-only structural evidence, omit money/customer/contact data.
import sys,pathlib,json,datetime
R=pathlib.Path('/opt/mp-analytics');sys.path.insert(0,str(R))
from core import db
import psycopg2,psycopg2.extras
O=pathlib.Path(__file__).parent/'snapshot'
c=psycopg2.connect(db.DATABASE_URL);c.set_session(readonly=True,isolation_level='REPEATABLE READ')
out={'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'queries':{}}
qs={
'shipment_bom':"SELECT external_code,components,kind,source,source_sha256,resolved_at,valid_until FROM ms_shipment_bom",
'assessment_coverage':"SELECT platform,account,count(*) n FROM ms_shipment_assessments WHERE is_current GROUP BY platform,account",
'shipment_examples':"SELECT demand_id,document_name,document_moment,account,platform,status,raw_document->'positions' positions,raw_order->'positions' order_positions FROM ms_shipment_assessments WHERE is_current AND account='oz_acc1' ORDER BY document_moment DESC LIMIT 30",
'posting_demand_position_examples':"SELECT p.posting_number,p.in_process_at,p.scheme,p.status,p.payload->'products' market_products,d.demand_id,d.demand_date,dp.ms_id,dp.qty physical_qty,m.external_code,m.code,m.name,m.updated_at FROM raw_ozon_posting p JOIN oz_cogs_demand d ON d.account=p.account AND d.demand_name=p.posting_number JOIN ms_demand_pos dp ON dp.demand_id=d.demand_id LEFT JOIN ms_product m ON m.ms_id=dp.ms_id WHERE p.account='oz_acc1' ORDER BY p.in_process_at DESC LIMIT 100"}
with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
 for k,q in qs.items():
  cur.execute(q);rows=[dict(r) for r in cur.fetchall()];out['queries'][k]={'sql':q,'rows':rows};print(k,len(rows))
c.rollback();c.close()
(O/'fulfillment_evidence.json').write_text(json.dumps(out,ensure_ascii=False,default=str))
