# поток: fin
"""Refresh the existing warehouse monitor without financial rebuilds or messages."""
from datetime import date, datetime, timezone
from pathlib import Path
import fcntl
import json

from core import db
from ops.fbs_ms_post_import_shipments import durable_save

RUNTIME = Path(__file__).resolve().parents[1] / '.private/mp_inventory_refresh'


def freeze_baseline():
    with db.get_conn() as con, con.cursor() as cur:
        cur.execute('''INSERT INTO mp_inventory_baseline_capture
          (platform,account,captured_at,source_recorded_at,source_rows,usable_available)
          SELECT DISTINCT ON (platform,account) platform,account,captured_at,recorded_at,
            source_rows,usable_available FROM mp_stock_capture
          ORDER BY platform,account,captured_at ON CONFLICT DO NOTHING
          RETURNING platform,account''')
        created = cur.fetchall()
        for platform, account in created:
            cur.execute('''INSERT INTO mp_inventory_baseline_item
          (platform,account,sku,warehouse,usable_available,raw_available,excluded_loss)
          SELECT h.platform,h.account,h.sku,h.warehouse,h.usable_available,h.raw_available,h.excluded_loss
          FROM mp_stock_history h JOIN mp_inventory_baseline_capture c
          ON c.platform=h.platform AND c.account=h.account AND c.captured_at=h.captured_at
          WHERE h.platform=%s AND h.account=%s
          ON CONFLICT DO NOTHING''', (platform, account))


def refresh(steps):
    results = {}
    for name, fn in steps:
        try:
            value = fn()
            results[name] = {'status': 'partial' if isinstance(value, dict) and value.get('source_errors') else 'ok', 'result': value}
        except Exception as exc:
            results[name] = {'status': 'error', 'error': type(exc).__name__}
        print(json.dumps({'step': name, 'status': results[name]['status']}), flush=True)
    return results


def collect_returns():
    from returns_bot.collect import gather, store
    rows, errors = gather()
    result = store(rows, mark_gone=False) if rows else {'heads': 0}
    # A partial response must never mark another return as disappeared.
    result['source_errors'] = errors
    return result


def main():
    from collectors.wb import collect_stocks
    from collectors.ozon_fbo_stock import main as ozon
    from collectors.market_ms_return_receipts import collect as receipts
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        today = date.today().isoformat()
        steps = [(a, lambda a=a: collect_stocks(a, today)) for a in ('wb_acc1', 'wb_acc2')]
        steps += [(a, lambda a=a: ozon(a)) for a in ('oz_acc1', 'oz_acc2')]
        steps += [('baseline', freeze_baseline), ('returns', collect_returns), ('market_receipts', receipts)]
        results = refresh(steps)
        durable_save(RUNTIME / 'latest.json', {'at': datetime.now(timezone.utc).isoformat(), 'steps': results})
        return int(any(r['status'] != 'ok' for r in results.values()))


if __name__ == '__main__':
    raise SystemExit(main())
