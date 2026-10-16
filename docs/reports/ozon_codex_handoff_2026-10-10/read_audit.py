# поток: mkt — read-only recovery audit, no marketplace mutations or DB writes
import sys, json, pathlib, datetime, csv
sys.path.insert(0, '/opt/mp-analytics')
import psycopg2, psycopg2.extras
from core import db

OUT = pathlib.Path(__file__).parent
conn = psycopg2.connect(db.DATABASE_URL)
conn.set_session(readonly=True, autocommit=True)

def query(sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute('SET statement_timeout = 120000')
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]

def save(name, data):
    (OUT / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))

def run_db():
    result = {}
    result['freshness'] = {}
    for table, day, loaded in [('mkt_ozon_ads_sku_daily','stat_date','collected_at'), ('ozon_bids','captured_at','captured_at'), ('raw_ozon_posting','in_process_at','loaded_at'),('raw_ozon_accrual','accrual_date','loaded_at'),('raw_ozon_transaction',"(payload->>'operation_date')::timestamp",'loaded_at'),('ad_spend_daily','date','updated_at'),('margin_by_sku','period_to','computed_at'),('ozon_price_index','collected_on','updated_at'),('ozon_search_product','period_end','updated_at')]:
        result['freshness'][table] = query(f"SELECT account,min({day}) first_day,max({day}) last_day,max({loaded}) last_loaded,count(*) rows FROM {table} WHERE account IN ('oz_acc1','oz_acc2') GROUP BY 1")
    result['ads_day_coverage'] = query("SELECT account,stat_date,count(*) rows,count(distinct sku) skus,sum(views) views,sum(clicks) clicks,sum(money_spent) spend FROM mkt_ozon_ads_sku_daily WHERE stat_date>='2026-08-01' GROUP BY 1,2 ORDER BY 1,2")
    result['journal_actions'] = query("SELECT account,action,count(*) rows,count(distinct sku) skus,count(*) FILTER(WHERE applied) applied,min(applied_at) first_apply,max(applied_at) last_apply FROM mkt_ozon_bid_journal GROUP BY 1,2 ORDER BY 1,2")
    result['recent_journal'] = query("SELECT * FROM mkt_ozon_bid_journal WHERE decided_on>='2026-09-18' OR applied_at>='2026-09-18' ORDER BY created_at")
    result['campaigns_history'] = query("SELECT account,campaign_id,campaign_title,min(captured_at) first_seen,max(captured_at) last_seen,count(distinct sku) skus,min(bid) min_bid,max(bid) max_bid FROM ozon_bids GROUP BY 1,2,3 ORDER BY 1,4")
    result['latest_campaigns'] = query("WITH d AS(SELECT account,max(captured_at) AS captured_on FROM ozon_bids GROUP BY 1) SELECT b.account,campaign_id,campaign_title,state,count(*) rows,min(bid) min_bid,max(bid) max_bid FROM ozon_bids b JOIN d ON b.account=d.account AND b.captured_at=d.captured_on GROUP BY 1,2,3,4 ORDER BY 1,2")
    result['ads_campaigns'] = query("SELECT account,campaign_id,min(stat_date) first_day,max(stat_date) last_day,sum(views) views,sum(clicks) clicks,sum(money_spent) spend,sum(orders_qty) ad_orders,sum(orders_money) ad_revenue FROM mkt_ozon_ads_sku_daily WHERE stat_date>='2026-09-18' GROUP BY 1,2 ORDER BY 1,2")
    result['ozon_ads'] = query("SELECT * FROM ozon_ads WHERE period>='2026-08-01' ORDER BY account,period,campaign_id")
    result['posting_status'] = query("SELECT account,scheme,status,count(*) postings FROM raw_ozon_posting WHERE in_process_at>='2026-08-01' GROUP BY 1,2,3 ORDER BY 1,2,3")
    result['posting_shape'] = query("SELECT account,jsonb_object_keys(payload) key FROM (SELECT DISTINCT ON(account) account,payload FROM raw_ozon_posting ORDER BY account,in_process_at DESC) r")
    result['product_shape'] = query("SELECT account,jsonb_object_keys(payload->'products'->0) key FROM (SELECT DISTINCT ON(account) account,payload FROM raw_ozon_posting WHERE jsonb_array_length(payload->'products')>0 ORDER BY account,in_process_at DESC) r")
    result['return_status'] = query("SELECT r.account,r.status_raw,r.status_name,r.stage,count(distinct r.return_id) returns,sum(i.qty) units FROM mp_returns r JOIN mp_return_items i USING(platform,account,return_id) WHERE platform='ozon' AND r.created_at>='2026-08-01' GROUP BY 1,2,3,4 ORDER BY 1,2")
    result['margin_months'] = query("SELECT account,period_from,period_to,count(*) rows,sum(qty) qty,sum(revenue_buyer) revenue,sum(cogs) cogs,count(*) FILTER(WHERE cogs=0 AND qty>0) zero_cogs_skus,sum(net_profit) net,max(computed_at) updated FROM margin_by_sku WHERE platform='ozon' AND period_from>='2026-08-01' GROUP BY 1,2,3 ORDER BY 1,2")
    dose_path=pathlib.Path('/opt/mp-analytics/docs/experiments/ozon_acc2_bid_dose_2026-09-21')
    assignment=list(csv.DictReader((dose_path/'assignment.csv').open()))
    result['dose_assignment']=assignment
    print('assignment columns:',list(assignment[0]))
    skus=[x['sku'] for x in assignment]
    result['dose_bids']=query("SELECT campaign_id,campaign_title,sku,captured_at,bid,state FROM ozon_bids WHERE account='oz_acc2' AND sku::text=ANY(%s) ORDER BY captured_at,sku",(skus,))
    result['dose_ads']=query("SELECT * FROM mkt_ozon_ads_sku_daily WHERE account='oz_acc2' AND sku::text=ANY(%s) AND stat_date>='2026-08-31' ORDER BY stat_date,sku",(skus,))
    save('db_audit',result)
    print('db audit saved:',len(result),'datasets; recent journal rows',len(result['recent_journal']))
    for r in result['latest_campaigns']:print(r)

def run_api():
    import requests
    from collectors import ozon_ads
    result={}
    for account in ['oz_acc1','oz_acc2']:
        headers={'Authorization':'Bearer '+ozon_ads._token(account)}
        r=requests.get(ozon_ads.PERF+'/api/client/campaign',headers=headers,timeout=60)
        r.raise_for_status(); camps=r.json().get('list',[])
        result[account]={'read_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'campaigns':camps,'statistics':{},'products':{}}
        print(account,'campaigns',len(camps))
        ids=[str(c['id']) for c in camps]
        for start in range(0,len(ids),10):
            r=requests.get(ozon_ads.PERF+'/api/client/statistics/daily/json',headers=headers,params={'campaignIds':ids[start:start+10],'dateFrom':'2026-09-18','dateTo':'2026-10-10'},timeout=120)
            result[account]['statistics'][str(start)]={'status':r.status_code,'body':r.json()}
        # Retrieve current membership and bids, preserving pagination evidence.
        for camp in camps:
            if camp.get('advObjectType') in ozon_ads.PAY_ORDER_TYPES:continue
            cid=str(camp['id']);pages=[]
            for page in range(1,1000):
                r=requests.get(ozon_ads.PERF+f'/api/client/campaign/{cid}/v2/products',headers=headers,params={'page':page,'pageSize':100},timeout=60)
                body=r.json();pages.append({'status':r.status_code,'body':body})
                if r.status_code!=200:break
                items=body.get('products',body.get('list',[]))
                if not items or len(items)<100:break
            result[account]['products'][cid]=pages
    save('api_audit',result)
    print('API audit saved (credentials excluded)')

if __name__=='__main__':
    if sys.argv[1]=='db':run_db()
    elif sys.argv[1]=='api':run_api()
