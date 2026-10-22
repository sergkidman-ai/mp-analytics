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


def installed():
    return bool(db.query("SELECT to_regclass('customer_guard_orders') AS name")[0]["name"])


@router.get("/warehouse/customer-orders", response_class=HTMLResponse)
def page():
    return HTMLResponse((Path(__file__).parent / "static/customer_orders.html").read_text(),
                        headers={"Cache-Control": "no-store"})


@router.get("/api/customer-orders")
def data(status: str = "", signal: str = "", search: str = "", product_code: str = "", account: str = "",
         limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    if not installed():
        return dict(installed=False, rows=[], total=0, sync=[], queue={})
    if account and account not in ACCOUNTS:
        raise HTTPException(422,'Неизвестный кабинет')
    bases={}
    for acc in ACCOUNTS:
        try:bases[acc]=get_basis(acc)
        except BasisUnavailable:bases[acc]=None
    where, params = ["o.account=ANY(%s)"], [list(ACCOUNTS)]
    if account:
        where.append('o.account=%s');params.append(account)
    if status:
        where.append("(o.ms_status=%s OR o.mp_status=%s)")
        params.extend([status, status])
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
      LEFT JOIN customer_guard_jobs j ON j.order_id=o.id """
    received_at = """coalesce(nullif(o.mp_payload->>'in_process_at','')::timestamptz,
      nullif(o.mp_payload->>'created_at','')::timestamptz)"""
    total = db.query("SELECT count(*) n " + base + "WHERE " + condition, tuple(params))[0]["n"]
    rows = db.query("""SELECT o.id,o.platform,o.account,o.posting_number,o.parent_number,o.mp_status,o.ms_status,o.ms_store,
      o.first_seen_at,o.observed_at,o.ms_due_at,o.ms_checked_at,o.ms_id,
      o.mp_payload->'products' products,c.result,c.calculated_at,
      coalesce(c.generation=o.generation,false) current,
      j.state job_state,j.error job_error,j.due_at retry_at, """ + received_at + " mp_received_at " + base +
      "WHERE " + condition + " ORDER BY coalesce(" + received_at + ",o.first_seen_at) DESC,o.id DESC LIMIT %s OFFSET %s",
      tuple(params + [limit, offset]))
    for row in rows:
        try:
            row["mp_price"] = str(sale({"products": row["products"]})[0])
        except ValueError:
            row["mp_price"] = None
    for row in rows:
        basis=bases.get(row['account'])
        row["basis_current"]=bool(basis and (row["result"] or {}).get("rule_version")==basis["version"])
    queue = db.query("""SELECT count(*) FILTER(WHERE state IN ('pending','retry','running')) pending,
      count(*) FILTER(WHERE state='error') errors,min(due_at) FILTER(WHERE state IN ('pending','retry')) next_due
      FROM customer_guard_jobs""")[0]
    runtime={}
    if db.query("SELECT to_regclass('customer_guard_runtime') name")[0]["name"]:
        runtime=db.query("""SELECT *,coalesce(heartbeat_at>now()-interval '10 minutes',false) healthy
          FROM customer_guard_runtime WHERE id""")[0]
    notices=db.query("SELECT delivery,count(*) n FROM customer_guard_incidents GROUP BY delivery")
    return dict(installed=True, rows=rows, total=total, queue=queue, runtime=runtime,
                notifications={x["delivery"]:x["n"] for x in notices},
                sync=db.query("SELECT * FROM customer_guard_sync_state ORDER BY scope"),
                automatic_updates=bool(runtime.get("enabled") and runtime.get("healthy")),
                notifications_enabled=bool(runtime.get("enabled") and runtime.get("healthy")
                                           and runtime.get("notifications_enabled")))


@router.get("/api/customer-orders/{order_id}/history")
def history(order_id: int):
    if not installed():
        raise HTTPException(503, "Хранилище ещё не установлено")
    return dict(rows=db.query("""SELECT id,generation,calculated_at,result
      FROM customer_guard_checks WHERE order_id=%s ORDER BY id DESC LIMIT 50""", (order_id,)))
