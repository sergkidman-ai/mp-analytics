# поток: fin
"""FBO marketplace order evidence. This module never reads or queues MySklad."""
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from psycopg2.extras import Json, RealDictCursor

from core import db
from core.ozon_posting import product_price
from collectors.ozon_postings import fetch_postings
from collectors.wb import _token as wb_token

WB_ORDERS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
WB_SALES_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"
WB_FEED_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v1/order-feed"
MOSCOW = timezone(timedelta(hours=3))
WB_STATUS = {"created": "created", "buyout": "delivered", "cancel": "cancelled",
             "return": "returned", "returnDefective": "returned_defective",
             "sales_buyout":"delivered", "sales_return":"returned"}
FBO_LOCK = 7348201790


def timestamp(value, *, naive_zone=timezone.utc):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=naive_zone) if result.tzinfo is None else result


def amount(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_fbo_price")
    return result


def normalized_ozon(post, account, observed_at):
    if not post.get("posting_number") or not post.get("status"):
        raise ValueError("invalid_ozon_fbo_order")
    created = timestamp(post.get("in_process_at") or post.get("created_at"))
    if created is None:
        raise ValueError("missing_ozon_fbo_date")
    products = []
    total = Decimal(0)
    for product in post.get("products") or []:
        qty = product.get("quantity")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise ValueError("invalid_ozon_fbo_quantity")
        price = product_price(product)
        if price < 0:
            raise ValueError("invalid_ozon_fbo_price")
        total += price * qty
        products.append({"offer_id": str(product.get("offer_id") or ""),
                         "sku": str(product.get("sku") or ""),
                         "name": product.get("name") or "", "quantity": qty})
    if not products or any(not p["offer_id"] or not p["sku"] for p in products):
        raise ValueError("invalid_ozon_fbo_products")
    return dict(platform="ozon", account=account, posting_number=post["posting_number"],
                parent_number=str(post.get("order_number") or ""), created_at=created,
                status_at=None, observed_at=observed_at, mp_status=post["status"],
                source_status=post["status"], warehouse=None, products=products,
                mp_price=total, price_source="ozon_fbo_list_product_price",
                source_api="ozon_fbo_list_v3", source_evidence={"substatus":post.get("substatus") or ""})


def normalized_wb(stat, feed, account, observed_at, sale=None):
    if stat.get("warehouseType") != "Склад WB":
        raise ValueError("not_wb_fbo")
    srid = str(stat.get("srid") or "")
    nm = stat.get("nmId")
    created = timestamp(stat.get("date"), naive_zone=MOSCOW)
    if not srid or not nm or created is None:
        raise ValueError("invalid_wb_fbo_identity")
    candidates=[]
    if feed and feed.get("status"):
        candidates.append((timestamp(feed.get("updatedAt"), naive_zone=MOSCOW) or created,3,
                           str(feed["status"]),"wb_order_feed+supplier_orders"))
    if sale and str(sale.get("saleID") or "").startswith(("S","R")):
        sale_status="sales_return" if str(sale["saleID"]).startswith("R") else "sales_buyout"
        candidates.append((timestamp(sale.get("date"), naive_zone=MOSCOW) or created,2,
                           sale_status,"wb_supplier_sales+supplier_orders"))
    if stat.get("isCancel"):
        candidates.append((timestamp(stat.get("cancelDate"), naive_zone=MOSCOW) or created,1,
                           "stats_cancelled","wb_supplier_orders"))
    if candidates:
        status_at,_,source_status,source_api=max(candidates)
    else:
        status_at=timestamp(stat.get("lastChangeDate"), naive_zone=MOSCOW)
        source_status,source_api="stats_unconfirmed","wb_supplier_orders"
    mp_status = WB_STATUS.get(source_status, "cancelled" if source_status == "stats_cancelled" else "status_unknown")
    price_source = "wb_order_feed_seller_price" if feed and feed.get("sellerPrice") is not None else "wb_orders_price_with_disc"
    price = feed.get("sellerPrice") if price_source == "wb_order_feed_seller_price" else stat.get("priceWithDisc")
    products = [{"offer_id": str(stat.get("supplierArticle") or ""),
                 "sku": str(nm), "name": "", "quantity": 1}]
    return dict(platform="wb", account=account, posting_number=srid,
                parent_number=str(stat.get("gNumber") or ""), created_at=created,
                status_at=status_at, observed_at=observed_at, mp_status=mp_status,
                source_status=source_status,
                warehouse=(feed or {}).get("warehouseName") or stat.get("warehouseName"),
                products=products, mp_price=amount(price), price_source=price_source,
                source_api=source_api,
                source_evidence={"warehouseType":stat["warehouseType"],
                                 "isCancel":bool(stat.get("isCancel")),
                                 "stat_lastChangeDate":stat.get("lastChangeDate"),
                                 "feed_updatedAt":(feed or {}).get("updatedAt"),
                                 "sales_date":(sale or {}).get("date"),
                                 "sale_type":str((sale or {}).get("saleID") or "")[:1]})


def fingerprint(row):
    fields = (row["mp_status"], row["source_status"], row["warehouse"], row["products"],
              str(row["mp_price"]), row["price_source"], row["source_api"], row["source_evidence"])
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def record_orders(scope, rows, started_at, completed_at, window_from, window_to, source_rows, skipped_rows=0):
    """A fully fetched account window commits atomically; no customer_guard_jobs writes."""
    changed = 0
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for row in rows:
                key = (row["platform"],row["account"],row["posting_number"])
                cur.execute("""SELECT id,input_hash,status_at,observed_at FROM mp_fbo_orders
                  WHERE platform=%s AND account=%s AND posting_number=%s FOR UPDATE""",key)
                old = cur.fetchone()
                if old and old["status_at"] and row["status_at"] and row["status_at"] < old["status_at"]:
                    continue
                fp = fingerprint(row)
                if old and old["input_hash"] == fp:
                    cur.execute("UPDATE mp_fbo_orders SET observed_at=GREATEST(observed_at,%s) WHERE id=%s",
                                (row["observed_at"],old["id"]))
                    continue
                params = (row["parent_number"],row["created_at"],row["status_at"],
                          row["observed_at"],row["mp_status"],row["source_status"],
                          row["warehouse"],Json(row["products"]),row["mp_price"],row["price_source"],
                          row["source_api"],Json(row["source_evidence"]),fp)
                if old:
                    cur.execute("""UPDATE mp_fbo_orders SET parent_number=%s,created_at=%s,status_at=%s,
                      observed_at=%s,mp_status=%s,source_status=%s,warehouse=%s,products=%s,mp_price=%s,
                      price_source=%s,source_api=%s,source_evidence=%s,input_hash=%s WHERE id=%s""",
                      params+(old["id"],))
                    order_id = old["id"]
                else:
                    cur.execute("""INSERT INTO mp_fbo_orders
                      (platform,account,posting_number,parent_number,created_at,status_at,observed_at,
                       mp_status,source_status,warehouse,products,mp_price,price_source,source_api,
                       source_evidence,input_hash)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                      key+params)
                    order_id = cur.fetchone()["id"]
                cur.execute("""INSERT INTO mp_fbo_order_events
                  (order_id,observed_at,status_at,mp_status,source_status,warehouse,products,mp_price,
                   source_api,source_evidence,input_hash)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT(order_id,input_hash) DO NOTHING""",
                  (order_id,row["observed_at"],row["status_at"],row["mp_status"],
                   row["source_status"],row["warehouse"],Json(row["products"]),
                   row["mp_price"],row["source_api"],Json(row["source_evidence"]),fp))
                changed += 1
            cur.execute("""INSERT INTO mp_fbo_sync_state
              (scope,started_at,completed_at,window_from,window_to,source_rows,saved_rows,skipped_rows)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(scope) DO UPDATE SET started_at=excluded.started_at,
              completed_at=excluded.completed_at,window_from=excluded.window_from,
              window_to=excluded.window_to,source_rows=excluded.source_rows,
              saved_rows=excluded.saved_rows,skipped_rows=excluded.skipped_rows""",
              (scope,started_at,completed_at,window_from,window_to,source_rows,len(rows),skipped_rows))
    return {"source_rows":source_rows,"saved_rows":len(rows),"changed_rows":changed,"skipped_rows":skipped_rows}


def backfill_ozon_raw():
    now=datetime.now(timezone.utc)
    result={}
    for account in ("oz_acc1","oz_acc2"):
        source=db.query("""SELECT payload,loaded_at FROM raw_ozon_posting
          WHERE scheme='fbo' AND account=%s ORDER BY in_process_at""",(account,))
        rows=[normalized_ozon(r["payload"],account,r["loaded_at"]) for r in source]
        result[account]=record_orders(f"ozon/{account}/fbo/backfill",rows,now,now,None,None,len(source))
    return result


def sync_ozon(account, *, days=90):
    started=datetime.now(timezone.utc)
    start=started-timedelta(days=days)
    # The live FBO v3 endpoint currently rejects limit>100 even though the
    # shared adapter permits larger values for historical callers.
    posts=fetch_postings(account,"fbo",start,started,limit=100,max_pages=30,window_budget=240)
    observed=datetime.now(timezone.utc)
    rows=[normalized_ozon(p,account,observed) for p in posts]
    return record_orders(f"ozon/{account}/fbo",rows,started,observed,start,started,len(posts))


def _wb_request(method,url,account,**kwargs):
    headers={"Authorization":wb_token(account)}
    for attempt in range(3):
        response=requests.request(method,url,headers=headers,timeout=90,**kwargs)
        if response.status_code==429 and attempt<2:
            try: pause=min(65,max(60,int(response.headers.get("Retry-After","60"))))
            except ValueError: pause=60
            time.sleep(pause)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("wb_fbo_api_retry_exhausted")


def fetch_wb_orders(account,now):
    start=(now-timedelta(days=89)).date().isoformat()
    data=_wb_request("GET",WB_ORDERS_URL,account,params={"dateFrom":start,"flag":0})
    if not isinstance(data,list):
        raise ValueError("invalid_wb_orders_response")
    return data


def fetch_wb_sales(account,now):
    start=(now-timedelta(days=89)).date().isoformat()
    data=_wb_request("GET",WB_SALES_URL,account,params={"dateFrom":start,"flag":0})
    if not isinstance(data,list):
        raise ValueError("invalid_wb_sales_response")
    return data


def fetch_wb_feed(account,now,*,limit=5000):
    local=now.astimezone(MOSCOW)
    start=local-timedelta(days=30)
    rows=[]
    snapshot=None
    for page in range(20):
        pagination={"limit":limit,"offset":len(rows)}
        if snapshot: pagination["snapshotTime"]=snapshot
        body={"selectedPeriod":{"start":start.isoformat(),"end":local.isoformat()},
              "nmIds":[],"subjectIds":[],"brandNames":[],"tagIds":[],"pagination":pagination}
        result=_wb_request("POST",WB_FEED_URL,account,json=body)
        data=result.get("data") or {}
        part=data.get("orders")
        if not isinstance(part,list):
            raise ValueError("invalid_wb_order_feed_response")
        rows.extend(part)
        snapshot=data.get("snapshotTime") or snapshot
        if len(part)<limit:
            return rows
        time.sleep(61)  # Official Order Feed limit: one request per minute per account.
    raise ValueError("wb_order_feed_page_limit")


def sync_wb(account):
    started=datetime.now(timezone.utc)
    stats=fetch_wb_orders(account,started)
    sales=fetch_wb_sales(account,started)
    feed=fetch_wb_feed(account,started)
    by_srid={str(x.get("srid")):x for x in feed if x.get("srid")}
    sales_by_srid={}
    for sale in sales:
        if sale.get("warehouseType")!="Склад WB" or not sale.get("srid"):
            continue
        key=str(sale["srid"])
        old=sales_by_srid.get(key)
        if old is None or (timestamp(sale.get("date"),naive_zone=MOSCOW) or started) >= (
                timestamp(old.get("date"),naive_zone=MOSCOW) or started):
            sales_by_srid[key]=sale
    fbo=[x for x in stats if x.get("warehouseType")=="Склад WB"]
    rows=[normalized_wb(x,by_srid.get(str(x.get("srid"))),account,datetime.now(timezone.utc),
                        sales_by_srid.get(str(x.get("srid")))) for x in fbo]
    completed=datetime.now(timezone.utc)
    stat_ids={str(s.get("srid")) for s in stats if s.get("srid")}
    skipped=sum(1 for x in feed if x.get("srid") and str(x["srid"]) not in stat_ids)
    return record_orders(f"wb/{account}/fbo",rows,started,completed,
                         started-timedelta(days=89),started,len(stats)+len(sales)+len(feed),skipped)


def main(platform="all"):
    if platform not in ("all","ozon","wb"):
        raise ValueError("invalid_fbo_platform")
    results={}
    with db.get_conn() as conn:
        conn.autocommit=True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)",(FBO_LOCK,))
                if not cur.fetchone()[0]:
                    return {"busy":True}
            if platform in ("all","ozon"):
                for account in ("oz_acc1","oz_acc2"):
                    results[account]=sync_ozon(account)
            if platform in ("all","wb"):
                for account in ("wb_acc1","wb_acc2"):
                    results[account]=sync_wb(account)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)",(FBO_LOCK,))
    return results


if __name__=="__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv)>1 else "all"),ensure_ascii=False,default=str))
