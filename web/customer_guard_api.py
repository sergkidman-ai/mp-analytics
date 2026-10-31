# поток: fin
"""Dashboard reads local data only. No API traffic from browser refreshes."""
from pathlib import Path

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse
from core import db
from customer_guard.domain import sale, ACCOUNTS
from core.ozon_expense_basis import BasisUnavailable
from customer_guard.rules import expense_basis as get_basis

router = APIRouter(tags=["Заказы покупателей"])
NEGATIVE_CODES = "('cancelled','returned','returned_defective')"
FBS_DETAIL = "coalesce(nullif(o.mp_payload->>'substatus',''),o.mp_status)"


def fbo_stock_movements(order_ids):
    """Observed stock intervals linked to FBO orders; never a COGS assignment."""
    if not order_ids:
        return {}
    sources = (
        ("mp_fbo_stock_link_candidates", """SELECT l.order_id,l.sku,l.quantity,l.source_warehouse,
          p.recorded_at from_at,c.recorded_at to_at,l.evidence,'to_client' direction
          FROM mp_fbo_stock_link_candidates l
          JOIN mp_fbo_orders o ON o.id=l.order_id
          JOIN mp_stock_capture p ON p.platform=o.platform AND p.account=l.account
            AND p.captured_at=l.previous_capture
          JOIN mp_stock_capture c ON c.platform=o.platform AND c.account=l.account
            AND c.captured_at=l.current_capture
          WHERE l.order_id=ANY(%s)"""),
        ("mp_fbo_cross_source_link_candidates", """SELECT l.order_id,l.sku,l.quantity,l.source_warehouse,
          p.recorded_at from_at,c.completed_at to_at,l.evidence,'to_client' direction
          FROM mp_fbo_cross_source_link_candidates l
          JOIN mp_fbo_orders o ON o.id=l.order_id
          JOIN mp_stock_capture p ON p.platform=o.platform AND p.account=l.account
            AND p.captured_at=l.previous_capture
          JOIN wb_fbo_intraday_capture c ON c.id=l.current_capture_id
          WHERE l.order_id=ANY(%s)"""),
        ("mp_fbo_intraday_link_candidates", """SELECT l.order_id,l.sku,l.quantity,l.source_warehouse,
          p.completed_at from_at,c.completed_at to_at,l.evidence,'to_client' direction
          FROM mp_fbo_intraday_link_candidates l
          JOIN wb_fbo_intraday_capture p ON p.id=l.previous_capture_id
          JOIN wb_fbo_intraday_capture c ON c.id=l.current_capture_id
          WHERE l.order_id=ANY(%s)"""),
    )
    available = {r['name'] for r in db.query("""SELECT name FROM unnest(%s::text[]) name
      WHERE to_regclass(name) IS NOT NULL""", ([name for name, _ in sources] +
      ['wb_return_journey_candidate'],))}
    movements = {order_id: [] for order_id in order_ids}
    for name, sql in sources:
        if name in available:
            for row in db.query(sql, (order_ids,)):
                movements[row['order_id']].append(row)
    if 'wb_return_journey_candidate' in available:
        for row in db.query("""SELECT order_id,sku,stage,previous_observed_at from_at,
          current_observed_at to_at,destination_observed_at,evidence
          FROM wb_return_journey_candidate WHERE order_kind='fbo' AND order_id=ANY(%s)""",
          (order_ids,)):
            base = dict(order_id=row['order_id'],sku=row['sku'],quantity=1,
                        source_warehouse=None,evidence=row['evidence'])
            movements[row['order_id']].append(dict(base,direction='from_client',
                from_at=row['from_at'],to_at=row['to_at']))
            if row['stage'] in ('received_wb','received_seller') and row['destination_observed_at']:
                movements[row['order_id']].append(dict(base,
                    direction='received_wb' if row['stage']=='received_wb' else 'received_seller',
                    from_at=row['to_at'],to_at=row['destination_observed_at']))
    for rows in movements.values():
        rows.sort(key=lambda row: row['to_at'])
    return movements


def installed():
    return bool(db.query("SELECT to_regclass('customer_guard_orders') AS name")[0]["name"])


@router.get("/warehouse/customer-orders", response_class=HTMLResponse)
def page():
    return HTMLResponse((Path(__file__).parent / "static/customer_orders.html").read_text(),
                        headers={"Cache-Control": "no-store"})


def negative_filter(value: str):
    """Return SQL predicates and bound parameters for FBS/FBO, respectively."""
    if not value:
        return (None, []), (None, [])
    if value == 'all':
        return (f'o.mp_status IN {NEGATIVE_CODES}', []), (f'mp_status IN {NEGATIVE_CODES}', [])
    if value == 'cancelled':
        return ("o.mp_status='cancelled'", []), ("mp_status='cancelled'", [])
    if value == 'returned':
        return ("o.mp_status IN ('returned','returned_defective')", []), ("mp_status IN ('returned','returned_defective')", [])
    parts = value.split(':', 2)
    if len(parts) != 3 or parts[0] not in ('fbs', 'fbo') or parts[1] not in ('ozon', 'wb', 'yandex') or not 0 < len(parts[2]) <= 120:
        raise HTTPException(422, 'Неизвестный отрицательный статус')
    scheme, platform, code = parts
    fbs = (f"o.mp_status IN {NEGATIVE_CODES} AND o.platform=%s AND {FBS_DETAIL}=%s", [platform, code])
    fbo = (f"mp_status IN {NEGATIVE_CODES} AND platform=%s AND source_status=%s", [platform, code])
    return (fbs if scheme == 'fbs' else ('false', [])), (fbo if scheme == 'fbo' else ('false', []))


def negative_options(fbo_available):
    """Only codes already observed in marketplace responses; no guessed taxonomy."""
    rows = db.query(f"""SELECT 'fbs' scheme,o.platform,{FBS_DETAIL} code,count(*) n
      FROM customer_guard_orders o WHERE o.mp_status IN {NEGATIVE_CODES}
      GROUP BY o.platform,{FBS_DETAIL}""")
    if fbo_available:
        rows += db.query(f"""SELECT 'fbo' scheme,platform,source_status code,count(*) n
          FROM mp_fbo_orders WHERE mp_status IN {NEGATIVE_CODES}
          GROUP BY platform,source_status""")
    return sorted(rows, key=lambda r:(r['platform'],r['scheme'],r['code'] or ''))


@router.get("/api/customer-orders")
def data(status: str = "", signal: str = "", search: str = "", product_code: str = "", account: str = "",
         scheme: str = "", negative_status: str = "", cancel_category: str = "",
         limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    if not installed():
        return dict(installed=False, rows=[], total=0, sync=[], queue={})
    if account and account not in ACCOUNTS:
        raise HTTPException(422,'Неизвестный кабинет')
    from customer_guard.cancellation import CATEGORIES
    if cancel_category and cancel_category not in CATEGORIES:
        raise HTTPException(422,'Неизвестный статус отмены')
    review_installed=bool(db.query("SELECT to_regclass('customer_guard_cancel_review') name")[0]['name'])
    if scheme not in ('','fbs','fbo'):
        raise HTTPException(422,'Неизвестная схема отгрузки')
    (negative_fbs, negative_fbs_params), (negative_fbo, negative_fbo_params) = negative_filter(negative_status)
    bases={}
    for acc in ACCOUNTS:
        try:bases[acc]=get_basis(acc)
        except BasisUnavailable:bases[acc]=None
    where, params = ["o.account=ANY(%s)"], [list(ACCOUNTS)]
    if account:
        where.append('o.account=%s');params.append(account)
    if negative_fbs:
        where.append('(' + negative_fbs + ')');params.extend(negative_fbs_params)
    if cancel_category:
        where.append('r.category=%s' if review_installed else 'false')
        if review_installed:params.append(cancel_category)
    if status:
        where.append("(o.ms_status=%s OR o.mp_status=%s)")
        params.extend([status, 'delivered' if status=='Доставлен' else status])
    if signal == "non_positive_profit":
        where.append("""c.generation=o.generation AND j.error IS NULL
          AND c.result->>'rule_version'=(%s::jsonb->>o.account)
          AND c.result->>'floor' IS NOT NULL
          AND CASE WHEN c.result->>'estimated_net' ~ '^-?[0-9]+([.][0-9]+)?$'
              THEN (c.result->>'estimated_net')::numeric <= 0 ELSE false END""")
        params.append(__import__('json').dumps({a:bases[a]['version'] if bases[a] else None for a in ACCOUNTS}))
    elif signal:
        where.append("c.generation=o.generation AND c.result->>'signal'=%s")
        params.append(signal)
    if search:
        where.append("strpos(o.posting_number,%s)>0")
        params.append(search[:80])
    if product_code.strip():
        code = product_code.strip().lower()
        where.append("""(EXISTS (
          SELECT 1 FROM jsonb_array_elements(coalesce(c.result->'positions','[]'::jsonb)) p
          WHERE lower(p->>'code')=%s OR lower(p->>'external_code')=%s)
          OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(coalesce(o.mp_payload->'products','[]'::jsonb)) p
          WHERE lower(p->>'offer_id')=%s))""")
        params.extend([code, code, code])
    condition = " AND ".join(where)
    base = """FROM customer_guard_orders o
      LEFT JOIN customer_guard_checks c ON c.id=o.latest_check_id
      LEFT JOIN customer_guard_jobs j ON j.order_id=o.id """ + (
      "LEFT JOIN customer_guard_cancel_review r ON r.order_id=o.id " if review_installed else '')
    review_cols=("r.category cancel_category,r.first_handoff_at,r.first_cancelled_at,r.buyer_received_at,"
      if review_installed else "NULL::text cancel_category,NULL::timestamptz first_handoff_at,"
        "NULL::timestamptz first_cancelled_at,NULL::timestamptz buyer_received_at,")
    received_at = """coalesce(nullif(o.mp_payload->>'in_process_at','')::timestamptz,
      nullif(o.mp_payload->>'created_at','')::timestamptz)"""
    total_fbs = (db.query("SELECT count(*) n " + base + "WHERE " + condition, tuple(params))[0]["n"]
                 if scheme!='fbo' else 0)
    rows = db.query("""SELECT o.id,o.platform,o.account,o.posting_number,o.parent_number,o.mp_status,o.ms_status,o.ms_store,
      o.first_seen_at,o.observed_at,o.ms_due_at,o.ms_checked_at,o.ms_id,"""+review_cols+"""
      o.mp_payload->'products' products,o.mp_payload->>'substatus' source_status,c.result,c.calculated_at,
      coalesce(c.generation=o.generation,false) current,
      j.state job_state,j.error job_error,j.due_at retry_at, """ + received_at + " mp_received_at " + base +
      "WHERE " + condition + " ORDER BY coalesce(" + received_at + ",o.first_seen_at) DESC,o.id DESC LIMIT %s",
      tuple(params + [limit+offset])) if scheme!='fbo' else []
    for row in rows:
        row['scheme']='fbs'
        try:
            row["mp_price"] = str(sale({"products": row["products"]})[0])
        except ValueError:
            row["mp_price"] = None
    for row in rows:
        basis=bases.get(row['account'])
        row["basis_current"]=bool(basis and (row["result"] or {}).get("rule_version")==basis["version"])
    total_fbo = 0
    fbo_available = bool(db.query("SELECT to_regclass('mp_fbo_orders') AS name")[0]['name'])
    if scheme!='fbs' and fbo_available:
        fwhere, fparams = ["account=ANY(%s)"], [[a for a in ACCOUNTS if a.startswith(('oz_','wb_'))]]
        if cancel_category:fwhere.append('false')
        if account:
            fwhere.append('account=%s');fparams.append(account)
        if negative_fbo:
            fwhere.append('(' + negative_fbo + ')');fparams.extend(negative_fbo_params)
        if status:
            normalized='delivered' if status=='Доставлен' else status
            if normalized=='returned':
                fwhere.append("mp_status IN ('returned','returned_defective')")
            else:
                fwhere.append('mp_status=%s');fparams.append(normalized)
        if signal and signal!='cost_unconfirmed':
            fwhere.append('false')
        if search:
            fwhere.append('strpos(posting_number,%s)>0');fparams.append(search[:80])
        if product_code.strip():
            code=product_code.strip().lower()
            fwhere.append("""EXISTS (SELECT 1 FROM jsonb_array_elements(products) p
              WHERE lower(p->>'offer_id')=%s OR lower(p->>'offer_id')=%s)""")
            fparams.extend([code,code+'wb'])
        fcondition=' AND '.join(fwhere)
        total_fbo=db.query('SELECT count(*) n FROM mp_fbo_orders WHERE '+fcondition,tuple(fparams))[0]['n']
        fbo_rows=db.query("""SELECT id,platform,account,posting_number,parent_number,mp_status,
          source_status,source_evidence,created_at mp_received_at,observed_at,status_at,warehouse,
          products,mp_price,price_source FROM mp_fbo_orders WHERE """+fcondition+
          " ORDER BY created_at DESC,id DESC LIMIT %s",tuple(fparams+[limit+offset]))
        for row in fbo_rows:
            row.update(scheme='fbo',result=None,current=True,basis_current=False,
                       first_seen_at=row['observed_at'],ms_status=None,ms_store=None,
                       ms_due_at=None,ms_checked_at=None,job_state=None,job_error=None,
                       retry_at=None,cancel_category=None,first_handoff_at=None,
                       first_cancelled_at=None,buyer_received_at=None)
            row['mp_price']=str(row['mp_price']) if row['mp_price'] is not None else None
        rows.extend(fbo_rows)
    rows.sort(key=lambda r:(r['mp_received_at'] or r['first_seen_at'],r['scheme'],r['id']),reverse=True)
    rows=rows[offset:offset+limit]
    movements=fbo_stock_movements([r['id'] for r in rows if r['scheme']=='fbo'])
    for row in rows:
        if row['scheme']=='fbo':
            row['stock_movements']=movements[row['id']]
    total=total_fbs+total_fbo
    queue = db.query("""SELECT count(*) FILTER(WHERE state IN ('pending','retry','running')) pending,
      count(*) FILTER(WHERE state='error') errors,min(due_at) FILTER(WHERE state IN ('pending','retry')) next_due
      FROM customer_guard_jobs""")[0]
    runtime={}
    if db.query("SELECT to_regclass('customer_guard_runtime') name")[0]["name"]:
        runtime=db.query("""SELECT *,coalesce(heartbeat_at>now()-interval '10 minutes',false) healthy
          FROM customer_guard_runtime WHERE id""")[0]
    notices=db.query("SELECT delivery,count(*) n FROM customer_guard_incidents GROUP BY delivery")
    fbo_sync=(db.query('SELECT * FROM mp_fbo_sync_state ORDER BY scope') if fbo_available else [])
    return dict(installed=True, rows=rows, total=total, queue=queue, runtime=runtime,
                negative_statuses=negative_options(fbo_available),
                notifications={x["delivery"]:x["n"] for x in notices},
                sync=db.query("SELECT * FROM customer_guard_sync_state ORDER BY scope"),
                fbo_sync=fbo_sync,
                automatic_updates=bool(runtime.get("enabled") and runtime.get("healthy")),
                notifications_enabled=bool(runtime.get("enabled") and runtime.get("healthy")
                                           and runtime.get("notifications_enabled")))


@router.get("/api/customer-orders/{order_id}/history")
def history(order_id: int):
    if not installed():
        raise HTTPException(503, "Хранилище ещё не установлено")
    checks=db.query("""SELECT id,generation,calculated_at,result
      FROM customer_guard_checks WHERE order_id=%s ORDER BY id DESC LIMIT 50""", (order_id,))
    statuses=db.query("""SELECT fetched_at,mp_status,substatus FROM (
      SELECT id,fetched_at,payload->>'status' mp_status,payload->>'substatus' substatus,
        lag(payload->>'status') OVER (ORDER BY id) previous_status,
        lag(payload->>'substatus') OVER (ORDER BY id) previous_substatus
      FROM customer_guard_snapshots WHERE order_id=%s
        AND source IN ('ozon','wb','yandex') AND payload ? 'status') history
      WHERE mp_status IS DISTINCT FROM previous_status OR substatus IS DISTINCT FROM previous_substatus
      ORDER BY id DESC LIMIT 50""",(order_id,))
    reviews=(db.query("""SELECT category,observed_at,evidence
      FROM customer_guard_cancel_review_history WHERE order_id=%s ORDER BY id""",(order_id,))
      if db.query("SELECT to_regclass('customer_guard_cancel_review_history') name")[0]['name'] else [])
    return dict(rows=checks,statuses=statuses,cancel_reviews=reviews)


@router.get("/api/customer-orders/fbo/{order_id}/history")
def fbo_history(order_id: int):
    if not db.query("SELECT to_regclass('mp_fbo_order_events') AS name")[0]['name']:
        raise HTTPException(503,"История ФБО ещё не установлена")
    return dict(stock_movements=fbo_stock_movements([order_id]).get(order_id, []),
      rows=db.query("""SELECT observed_at,status_at,mp_status,source_status,
      source_evidence,warehouse,products,mp_price,source_api FROM mp_fbo_order_events
      WHERE order_id=%s ORDER BY id DESC LIMIT 50""",(order_id,)))
