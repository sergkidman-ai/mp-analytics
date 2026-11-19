"""Receive a read-only office snapshot over the existing SSH connection."""
from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import db


def main():
    data = json.load(sys.stdin)
    account, rows = data['account'], data['rows']
    if account not in ('oz_acc1', 'oz_acc2') or not 1 <= len(rows) <= 100:
        raise ValueError("Invalid snapshot")
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT current_database()')
        if cur.fetchone()[0] != 'rev_recovery_20261103':
            raise RuntimeError("Unexpected database")
        cur.execute("""CREATE TABLE IF NOT EXISTS rev_ozon_lk_review (
                     account text NOT NULL, review_id uuid NOT NULL, observed_at timestamptz NOT NULL,
                     body text NOT NULL, product text, rating text, reply_state text NOT NULL,
                     seller_reply text, PRIMARY KEY(account,review_id))""")
        for r in rows:
            stamp = datetime.fromisoformat(r['seen'])
            if stamp.tzinfo is None or r['state'] not in ('answered','unanswered','unknown'):
                raise ValueError("Invalid observation")
            cur.execute("""INSERT INTO rev_ozon_lk_review
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                         ON CONFLICT(account,review_id) DO UPDATE SET
                         observed_at=excluded.observed_at, body=excluded.body,product=excluded.product,
                         rating=excluded.rating,reply_state=excluded.reply_state,seller_reply=excluded.seller_reply
                         WHERE excluded.observed_at>rev_ozon_lk_review.observed_at""",
                        (account,str(UUID(r['id'])),stamp,r['text'],r['product'],r['rating'],r['state'],r['answer']))


if __name__ == '__main__':
    main()
