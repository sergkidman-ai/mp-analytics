# поток: fin
"""Bounded MS status-only reads for WB FBS cancellations linked to return movement."""
import argparse
import json
from datetime import datetime, timezone

from core import db
from customer_guard.service import exclusive
from customer_guard.sources import CustomerMS


def due_orders(limit=2, posting_number=None):
    if not 1 <= limit <= 5:
        raise ValueError('status_watch_limit_1_to_5')
    return db.query("""SELECT o.id,o.account,o.posting_number,o.ms_id,o.ms_status
      FROM customer_guard_orders o
      WHERE o.platform='wb' AND o.account IN ('wb_acc1','wb_acc2')
        AND o.mp_status='cancelled' AND o.ms_id IS NOT NULL
        AND (%s IS NULL OR o.posting_number=%s)
        AND (o.ms_status_seen_at IS NULL OR o.ms_status_seen_at<now()-interval '12 hours')
        AND EXISTS (SELECT 1 FROM wb_return_journey_candidate c
          WHERE c.order_kind='fbs' AND c.order_id=o.id)
      ORDER BY coalesce(o.ms_status_seen_at,o.ms_checked_at) ASC NULLS FIRST,o.id
      LIMIT %s""", (posting_number,posting_number,limit))


def run(limit=2, posting_number=None):
    rows=due_orders(limit,posting_number)
    if not rows:
        return dict(checked=0,changed=0)
    checked=changed=0
    with exclusive() as pulse:
        clients={}
        for row in rows:
            pulse()
            if row['account'] not in clients:
                clients[row['account']]=CustomerMS(account=row['account'],budget=limit+1)
            client=clients[row['account']]
            document=client.find_order(row['posting_number'])
            if not document or document.get('id')!=row['ms_id']:
                raise RuntimeError('ms_order_identity_changed')
            state=document.get('state',{}).get('name')
            if not state:
                raise RuntimeError('ms_state_missing')
            now=datetime.now(timezone.utc)
            updated=db.execute("""UPDATE customer_guard_orders
              SET ms_status=%s,ms_status_seen_at=%s
              WHERE id=%s AND ms_id=%s AND mp_status='cancelled'
                AND (ms_status_seen_at IS NULL OR ms_status_seen_at<%s-interval '12 hours')""",
              (state,now,row['id'],row['ms_id'],now))
            checked+=updated
            changed+=int(bool(updated and state!=row['ms_status']))
    return dict(checked=checked,changed=changed)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int,default=2)
    parser.add_argument('--posting-number')
    args=parser.parse_args()
    print(json.dumps(run(args.limit,args.posting_number),ensure_ascii=False))
