# поток: ev
"""Single long-poll consumer for promo completion buttons in @ds_prc_bot."""
import os
import time
import psycopg2
from dotenv import load_dotenv
from ops.wb_promo_ack import api, handle_callback


def process_updates(conn, updates):
    for update in updates:
        callback = update.get('callback_query')
        if callback:
            handle_callback(conn, callback)
        with conn.cursor() as cur:
            cur.execute('''UPDATE prc_promo_callback_cursor SET next_update_id=
                greatest(next_update_id,%s) WHERE id=1''',(update['update_id']+1,))
        conn.commit()


def main():
    load_dotenv('/opt/mp-analytics/.env')
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(51420261012)')
            if not cur.fetchone()[0]:
                raise RuntimeError('Promo callback listener is already running')
        conn.commit()
        print('Promo callback listener started',flush=True)
        while True:
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT next_update_id FROM prc_promo_callback_cursor WHERE id=1')
                    offset = cur.fetchone()[0]
                conn.commit()
                updates = api('getUpdates',dict(offset=offset,timeout=25,limit=20,
                    allowed_updates=['callback_query']),timeout=35)
                process_updates(conn,updates)
            except Exception as exc:
                conn.rollback()
                print(f'Promo callback retry: {type(exc).__name__}',flush=True)
                time.sleep(5)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
