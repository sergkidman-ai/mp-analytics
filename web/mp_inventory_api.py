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
        coverage="По Маркету доступны только FBS-остатки наших складов (снимок 22.08.2026). Подтверждённого остатка на складе Маркета нет; ноль не предполагаем.")


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
    return dict(rows=rows)


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
