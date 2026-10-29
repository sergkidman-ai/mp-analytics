# поток: fin
"""Evidence-only daily FBO inventory. A stock balance never proves unit COGS."""
from datetime import date
from decimal import Decimal

from psycopg2.extras import execute_values

from core import db


def _number(value):
    return Decimal(str(value or 0))


def record_snapshot(platform, account, captured_at, rows):
    """Atomically replace this account's daily snapshot from one successful API response."""
    if platform not in ("wb", "ozon"):
        raise ValueError("unknown_stock_platform")
    day = date.fromisoformat(captured_at) if isinstance(captured_at, str) else captured_at
    if platform == "wb":
        acts = db.query("""SELECT d.warehouse,i.nm_id,i.qty FROM wb_loss_document d
          JOIN wb_loss_item i ON i.document_id=d.id WHERE d.account=%s
          AND (d.header_date IS NULL OR d.header_date<=%s)""", (account, day))
        losses = {}
        for row in acts:
            key = row["warehouse"], str(row["nm_id"])
            losses[key] = losses.get(key, Decimal(0)) + _number(row["qty"])
    else:
        losses = {}
    data = []
    for row in rows:
        sku = str(row["nm_id"] if platform == "wb" else row["sku"])
        warehouse = row["warehouse"]
        raw = _number(row["quantity"] if platform == "wb" else row["free_to_sell"])
        excluded = min(raw, losses.get((warehouse, sku), Decimal(0))) if platform == "wb" else Decimal(0)
        total = _number(row["quantity_full"]) if platform == "wb" else None
        data.append((platform, account, sku, warehouse, day,
                     "wb_stocks" if platform == "wb" else "ozon_fbo_stock",
                     raw, excluded, raw - excluded, total,
                     max(total - excluded, Decimal(0)) if total is not None else None,
                     _number(row.get("reserved")) if platform == "ozon" else None,
                     _number(row.get("promised")) if platform == "ozon" else None,
                     _number(row.get("in_way_to_client")) if platform == "wb" else None,
                     _number(row.get("in_way_from_client")) if platform == "wb" else None))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mp_stock_history WHERE platform=%s AND account=%s AND captured_at=%s",
                        (platform, account, day))
            if data:
                execute_values(cur, """INSERT INTO mp_stock_history
                  (platform,account,sku,warehouse,captured_at,source_table,raw_available,
                   excluded_loss,usable_available,raw_total,usable_total,reserved,promised,
                   in_way_to_client,in_way_from_client) VALUES %s""", data, page_size=500)
            cur.execute("""INSERT INTO mp_stock_capture
              (platform,account,captured_at,source_rows,raw_available,excluded_loss,usable_available)
              VALUES (%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(platform,account,captured_at) DO UPDATE SET
              source_rows=excluded.source_rows,raw_available=excluded.raw_available,
              excluded_loss=excluded.excluded_loss,usable_available=excluded.usable_available,
              recorded_at=now()""",
              (platform, account, day, len(data), sum((r[6] for r in data), Decimal(0)),
               sum((r[7] for r in data), Decimal(0)), sum((r[8] for r in data), Decimal(0))))
    return len(data)


def backfill():
    """Import existing dated source rows; do not fabricate missing zero days or costs."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO mp_stock_history
              (platform,account,sku,warehouse,captured_at,source_table,raw_available,
               excluded_loss,usable_available,raw_total,usable_total,in_way_to_client,in_way_from_client)
              SELECT 'wb',account,nm_id::text,warehouse,captured_at,'wb_stocks',
                COALESCE(quantity,0),excluded_loss,usable_quantity,
                quantity_full,usable_quantity_full,in_way_to_client,in_way_from_client
              FROM wb_stocks_usable ON CONFLICT DO NOTHING""")
            wb = cur.rowcount
            cur.execute("""INSERT INTO mp_stock_history
              (platform,account,sku,warehouse,captured_at,source_table,raw_available,
               excluded_loss,usable_available,reserved,promised)
              SELECT 'ozon',account,sku,warehouse,captured_at,'ozon_fbo_stock',
                COALESCE(free_to_sell,0),0,COALESCE(free_to_sell,0),reserved,promised
              FROM ozon_fbo_stock ON CONFLICT DO NOTHING""")
            ozon = cur.rowcount
            cur.execute("""INSERT INTO mp_stock_capture
              (platform,account,captured_at,source_rows,raw_available,excluded_loss,usable_available)
              SELECT platform,account,captured_at,count(*),sum(raw_available),
                sum(excluded_loss),sum(usable_available)
              FROM mp_stock_history GROUP BY platform,account,captured_at
              ON CONFLICT DO NOTHING""")
    return {"wb_rows": wb, "ozon_rows": ozon}


if __name__ == "__main__":
    print(backfill())
