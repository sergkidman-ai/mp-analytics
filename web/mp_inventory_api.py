# поток: fin
"""Read-only marketplace stock evidence and return status history for Warehouse."""
from collections import defaultdict
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from core import db

router = APIRouter(tags=["Склад МП"])
ACCOUNTS = {
    "oz_acc1": ("ozon", "Цифровой"), "oz_acc2": ("ozon", "Дисквер"),
    "wb_acc1": ("wb", "Цифровой"), "wb_acc2": ("wb", "Дисквер"),
    "ya_acc1": ("yandex", "Цифровой"),
}


def _date(value):
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "Дата должна быть в формате ГГГГ-ММ-ДД") from None


@router.get("/warehouse/marketplace-inventory", response_class=HTMLResponse)
def page():
    return HTMLResponse((Path(__file__).parent / "static/mp_inventory.html").read_text(),
                        headers={"Cache-Control": "no-store"})


def stock_data(on="", platform="", organization="", account="", warehouse="", sku="", limit=100, offset=0):
    if platform not in ("", "ozon", "wb", "yandex") or account and account not in ACCOUNTS:
        raise HTTPException(422, "Неизвестная площадка или кабинет")
    if organization not in ("", "1", "2"):
        raise HTTPException(422, "Неизвестная организация")
    day = _date(on)
    cap = db.query("""SELECT platform,account,min(captured_at) first,max(captured_at) current
      FROM mp_stock_capture WHERE captured_at<=%s GROUP BY platform,account ORDER BY platform,account""", (day,))
    frozen = {(r['platform'],r['account']):r for r in db.query(
        "SELECT platform,account,captured_at FROM mp_inventory_baseline_capture")}
    dates = {}
    for row in cap:
        p, a = row['platform'], row['account']
        previous = db.query("""SELECT max(captured_at) d FROM mp_stock_capture
          WHERE platform=%s AND account=%s AND captured_at<%s""",(p,a,row['current']))[0]['d']
        freeze = frozen.get((p,a),{}).get('captured_at')
        dates[(p,a)] = {"baseline":freeze if freeze and freeze<=row['current'] else row['first'],
                        "frozen":bool(freeze and freeze<=row['current']),
                        "previous":previous, "current":row['current']}
    selections = [(p,a,d) for (p,a),marks in dates.items()
                  for d in {marks['current'],marks['previous'],marks['baseline'] if not marks['frozen'] else None} if d]
    source = []
    if selections:
        placeholders = ",".join(["(%s,%s,%s)"] * len(selections))
        source = db.query("""SELECT platform,account,sku,warehouse,captured_at,raw_available,
          excluded_loss,usable_available,raw_total,usable_total,reserved,promised,
          in_way_to_client,in_way_from_client FROM mp_stock_history
          WHERE (platform,account,captured_at) IN (""" + placeholders + ")",
          tuple(v for triplet in selections for v in triplet))
    items = defaultdict(dict)
    warehouses = set()
    for row in source:
        key = (row['platform'],row['account'],row['sku'],row['warehouse'])
        items[key][str(row['captured_at'])] = row
        if row['captured_at'] == dates[key[:2]]['current']:
            warehouses.add((row['platform'],row['warehouse']))
    frozen_items = {}
    for row in db.query("""SELECT platform,account,sku,warehouse,usable_available
      FROM mp_inventory_baseline_item"""):
        key = row['platform'],row['account'],row['sku'],row['warehouse']
        if key[:2] in dates and dates[key[:2]]['frozen']:
            frozen_items[key] = row
            items.setdefault(key,{})
    out = []
    for (p,a,s,w), versions in items.items():
        if platform and p!=platform or account and a!=account or warehouse and w!=warehouse:
            continue
        if organization and not a.endswith('acc'+organization):
            continue
        if sku and sku.lower() not in s.lower():
            continue
        marks=dates[(p,a)]
        current=versions.get(str(marks['current']),{})
        prev=versions.get(str(marks['previous']),{})
        base=frozen_items.get((p,a,s,w),{}) if marks['frozen'] else versions.get(str(marks['baseline']),{})
        qty=current.get('usable_available') or 0
        previous=prev.get('usable_available') or 0
        initial=base.get('usable_available') or 0
        out.append(dict(platform=p,account=a,organization=ACCOUNTS.get(a,(None,a))[1],
            sku=s,warehouse=w,current=str(qty),previous=str(previous),initial=str(initial),
            change=str(qty-previous),since_baseline=str(qty-initial),
            raw=str(current.get('raw_available') or 0),excluded=str(current.get('excluded_loss') or 0),
            total=str(current['usable_total']) if current.get('usable_total') is not None else None,
            reserved=str(current['reserved']) if current.get('reserved') is not None else None,
            promised=str(current['promised']) if current.get('promised') is not None else None,
            in_way_to_client=str(current['in_way_to_client']) if current.get('in_way_to_client') is not None else None,
            in_way_from_client=str(current['in_way_from_client']) if current.get('in_way_from_client') is not None else None,
            captured_at=marks['current'],previous_at=marks['previous'],baseline_at=marks['baseline']))
    out.sort(key=lambda x:(abs(float(x['change'])),float(x['current']),x['sku']), reverse=True)
    summary = []
    for (p,a),marks in dates.items():
        if platform and p!=platform or account and a!=account:
            continue
        if organization and not a.endswith('acc'+organization):
            continue
        matching=[x for x in out if x['platform']==p and x['account']==a]
        summary.append(dict(platform=p,account=a,organization=ACCOUNTS.get(a,(None,a))[1],
            captured_at=marks['current'],baseline_at=marks['baseline'],
            available=str(sum(float(x['current']) for x in matching)),
            change=str(sum(float(x['change']) for x in matching))))
    return dict(rows=out[offset:offset+limit],total=len(out),summary=summary,
        warehouses=[dict(platform=p,name=w) for p,w in sorted(warehouses)],
        coverage="Маркет не включён в учёт остатков складов МП: возвраты поступают на наш склад. Их приёмка сверяется ниже по документам МС.")


@router.get("/api/warehouse/marketplace-stock")
def stock(on: str = "", platform: str = "", organization: str = "", account: str = "", warehouse: str = "", sku: str = "",
          limit: int = Query(100,ge=1,le=200), offset: int = Query(0,ge=0)):
    return stock_data(on,platform,organization,account,warehouse,sku[:100],limit,offset)


@router.get("/api/warehouse/marketplace-stock/history")
def stock_history(platform: str, account: str, sku: str, warehouse: str):
    if account not in ACCOUNTS or ACCOUNTS[account][0]!=platform or not sku or not warehouse:
        raise HTTPException(422,"Неизвестный товар или кабинет")
    rows=db.query("""SELECT captured_at,usable_available,raw_available,excluded_loss,
      usable_total,reserved,promised,in_way_to_client,in_way_from_client
      FROM mp_stock_history WHERE platform=%s AND account=%s AND sku=%s AND warehouse=%s
      ORDER BY captured_at DESC LIMIT 180""",(platform,account,sku,warehouse))
    links=[]
    intraday=[]
    if platform == 'wb':
        links=db.query("""SELECT l.previous_capture,l.current_capture,l.quantity,l.evidence,
          o.posting_number,o.created_at,o.mp_status
          FROM mp_fbo_stock_link_candidates l JOIN mp_fbo_orders o ON o.id=l.order_id
          WHERE l.account=%s AND l.sku=%s AND l.source_warehouse=%s
          ORDER BY l.current_capture DESC LIMIT 30""",(account,sku,warehouse))
        if account == 'wb_acc1':
            intraday=db.query("""SELECT c.completed_at,c.source_rows,
              coalesce(sum(i.quantity),0) quantity,
              coalesce(sum(i.in_way_to_client),0) in_way_to_client,
              coalesce(sum(i.in_way_from_client),0) in_way_from_client
              FROM (SELECT id,completed_at,source_rows FROM wb_fbo_intraday_capture
                    WHERE account=%s ORDER BY completed_at DESC LIMIT 30) c
              LEFT JOIN wb_fbo_intraday_item i ON i.capture_id=c.id AND i.nm_id=%s
              GROUP BY c.id,c.completed_at,c.source_rows
              ORDER BY c.completed_at DESC""",(account,int(sku))) if sku.isdigit() and len(sku)<=18 else []
    return dict(rows=rows,links=links,intraday=intraday)


@router.get("/api/warehouse/marketplace-returns")
def returns(platform: str = "", organization: str = "", account: str = "", source: str = "",
            status: str = "", stage: str = "",
            search: str = "", limit: int = Query(50,ge=1,le=200), offset: int = Query(0,ge=0)):
    if platform not in ("", "ozon", "wb", "yandex") or account and account not in ACCOUNTS:
        raise HTTPException(422,"Неизвестная площадка или кабинет")
    if stage not in ("", "pickup", "transit", "attention", "closed"):
        raise HTTPException(422,"Неизвестная стадия возврата")
    if source not in ("", "ozon", "ozon_rfbs", "ozon_removal", "wb", "yandex"):
        raise HTTPException(422,"Неизвестный источник возврата")
    if organization not in ("", "1", "2"):
        raise HTTPException(422,"Неизвестная организация")
    where=["true"];params=[]
    if organization:
        where.append("r.account=ANY(%s)")
        params.append([a for a in ACCOUNTS if a.endswith('acc'+organization)])
    for name,value in (("platform",platform),("account",account),("source",source),
                       ("stage",stage)):
        if value:
            where.append(f"r.{name}=%s");params.append(value)
    if status == '__empty__':
        where.append("coalesce(r.status_raw,'')=''")
    elif status:
        where.append('r.status_raw=%s');params.append(status)
    if search.strip():
        where.append("(r.order_number ILIKE %s OR r.return_id ILIKE %s OR raw.payload->>'posting_number' ILIKE %s)")
        q='%'+search.strip()[:80].replace('%','\\%').replace('_','\\_')+'%'
        params.extend([q,q,q])
    condition=' AND '.join(where)
    base="""FROM mp_returns r LEFT JOIN raw_mp_returns raw
      ON raw.platform=r.platform AND raw.account=r.account AND raw.return_id=r.return_id"""
    total=db.query("SELECT count(*) n "+base+" WHERE "+condition,tuple(params))[0]['n']
    rows=db.query("""SELECT r.platform,r.account,r.return_id,r.source,r.order_number,
      raw.payload->>'posting_number' posting_number,r.return_type,r.scheme,r.status_raw,
      r.status_name,r.stage,r.pvz_name,r.where_now,r.first_seen,r.last_seen,r.received_at
      """+base+" WHERE "+condition+" ORDER BY r.last_seen DESC,r.first_seen DESC LIMIT %s OFFSET %s",
      tuple(params+[limit,offset]))
    codes=db.query("""SELECT platform,source,status_raw,count(*) n FROM mp_returns
      GROUP BY platform,source,status_raw ORDER BY platform,source,status_raw""")
    catalog=[]
    if db.query("SELECT to_regclass('mp_return_status_events') name")[0]['name']:
        catalog=db.query("""SELECT platform,source,status_raw,status_evidence,
          count(*) observations,count(DISTINCT (account,return_id)) returns
          FROM mp_return_status_events GROUP BY platform,source,status_raw,status_evidence
          ORDER BY platform,source,status_raw,status_evidence""")
    return dict(rows=rows,total=total,statuses=codes,status_catalog=catalog)


@router.get("/api/warehouse/marketplace-returns/{platform}/{account}/{return_id}/history")
def return_history(platform: str, account: str, return_id: str):
    if account not in ACCOUNTS or ACCOUNTS[account][0]!=platform or len(return_id)>100:
        raise HTTPException(422,"Неизвестный возврат")
    rows=db.query("""SELECT observed_at,source_changed_at,source,status_raw,status_name,
      stage,status_evidence,place_name,target_name,is_baseline
      FROM mp_return_status_events WHERE platform=%s AND account=%s AND return_id=%s
      ORDER BY id DESC LIMIT 100""",(platform,account,return_id))
    return dict(rows=rows)


@router.get("/api/warehouse/marketplace-market-receipts")
def market_receipts(state: str = "unconfirmed", search: str = "",
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """Market returns: MP identifies the case; a linked posted MS document confirms receipt."""
    if state not in ("", "unconfirmed", "in_transit", "received", "unknown", "all"):
        raise HTTPException(422, "Неизвестный фильтр")
    scans = db.query("SELECT agent_id,earliest_scanned,last_success_at FROM market_ms_return_scan")
    coverage = max((x['earliest_scanned'] for x in scans), default=None) if len(scans) >= 2 else None
    last_scan = min((x['last_success_at'] for x in scans), default=None) if len(scans) >= 2 else None
    rows = db.query("""WITH cases AS (
        SELECT r.account,r.return_id,r.order_number,r.return_type,r.status_raw,r.stage,
          r.created_at,r.last_seen,'return'::text source_kind
        FROM mp_returns r WHERE r.platform='yandex' AND r.return_type IN ('RETURN','UNREDEEMED')
        UNION ALL
        SELECT s.account,'order:'||s.order_id,s.order_id,'CANCELLED_IN_DELIVERY',
          s.payload->>'status','attention',d.demand_date::timestamptz,s.loaded_at,
          'order_without_return'
        FROM raw_yandex_stats_order s JOIN ya_cogs_demand d
          ON d.account=s.account AND d.demand_name=s.order_id
        WHERE s.payload->>'status'='CANCELLED_IN_DELIVERY'
          AND NOT EXISTS (SELECT 1 FROM mp_returns r WHERE r.platform='yandex'
            AND r.account=s.account AND r.order_number=s.order_id)
      )
      SELECT r.return_id,r.order_number,r.return_type,r.status_raw,r.stage,
      r.created_at,r.last_seen,r.source_kind,m.salesreturn_id,m.demand_id,m.store_name,m.applicable,
      m.moment,m.ms_updated_at,y.demand_id AS known_demand_id
      FROM cases r
      LEFT JOIN ya_cogs_demand y ON y.account=r.account AND y.demand_name=r.order_number
      LEFT JOIN LATERAL (
        SELECT salesreturn_id,demand_id,store_name,applicable,moment,ms_updated_at
        FROM market_ms_return_receipts m
        WHERE m.account=r.account AND m.order_number=r.order_number
        ORDER BY (m.demand_id::text=y.demand_id) DESC NULLS LAST,
          COALESCE(m.applicable AND m.store_name IN
          ('Звездный','Дисквер','Кантемировская','Брак'),false) DESC,m.moment DESC
        LIMIT 1
      ) m ON true
      ORDER BY r.created_at DESC NULLS LAST""")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        result = market_receipt_result(row, coverage)
        age = (now - row['created_at']).days if row['created_at'] else None
        out.append({**row, 'result': result, 'age_days': age})
    if search.strip():
        term = search.strip()[:80].lower()
        out = [r for r in out if term in str(r['order_number'] or '').lower()
               or term in str(r['return_id']).lower()]
    if state == 'unconfirmed':
        out = [r for r in out if r['result'] in ('waiting','draft','other_store','mismatch')]
    elif state == 'in_transit':
        out = [r for r in out if r['result'] in ('waiting','draft','other_store','mismatch')
               and r['stage'] in ('transit','pickup','attention')]
    elif state == 'received':
        out = [r for r in out if r['result'] == 'received']
    elif state == 'unknown':
        out = [r for r in out if r['result'] == 'unknown']
    out.sort(key=lambda r: (r['result'] not in ('waiting','draft','other_store','mismatch'),
                            r['stage'] not in ('transit','pickup','attention'),
                            -(r['age_days'] or 0), str(r['order_number'])))
    return dict(rows=out[offset:offset+limit], total=len(out),
                coverage_since=coverage, last_ms_scan=last_scan)


def market_receipt_result(row, coverage):
    """A posted return to a known own store with the exact shipment link is receipt evidence."""
    if row['salesreturn_id']:
        if row['known_demand_id'] and str(row['demand_id']) != str(row['known_demand_id']):
            return 'mismatch'
        if not row['applicable']:
            return 'draft'
        if row['store_name'] not in ('Звездный', 'Дисквер', 'Кантемировская', 'Брак'):
            return 'other_store'
        return 'received'
    if not coverage or not row['created_at'] or row['created_at'].date() < coverage:
        return 'unknown'
    return 'waiting'
