# поток: print
"""Persistent counter ledger in an isolated PostgreSQL schema.

The UI term 'printed' means BOTH PDFs were published to the ready-file list,
or an explicit user confirmation was recorded in external_prints.
No printer confirmation or MP/MS writes.
Schema installation is explicit; importing this module never creates tables.
"""
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from print_service.domain import ACCOUNTS, MOSCOW, PrintBlocked, selected_order
from print_service.pdf import pdf_reader

LABEL_AVAILABILITY_ERRORS = (
    'wb_order_not_ready_for_label', 'ozon_posting_not_ready_for_label',
    'market_order_not_ready_for_label', 'ozon_scanit_missing',
    'market_order_identity_mismatch',
    'ozon_label_still_generating', 'ozon_label_generation_failed',
    'ozon_express_outside_fbs_scope', 'ozon_warehouse_color_unmapped',
    'ozon_label_task_result_uncertain', 'ozon_label_retry_pending', 'ozon_transport_error',
)


def checked_file(path, expected_sha=None):
    path = Path(path).resolve()
    try:
        data = path.read_bytes()
    except OSError:
        raise PrintBlocked('print_file_unavailable') from None
    digest = sha256(data).hexdigest()
    if expected_sha and digest != expected_sha:
        raise PrintBlocked('print_file_checksum_mismatch')
    pdf_reader(data)
    return str(path), digest


def moscow_day(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('clock_requires_timezone')
    return now.astimezone(MOSCOW).date()


class PrintQueue:
    def __init__(self, *, schema='print_ops', connect=None):
        if not re.fullmatch(r'print_[a-z0-9_]{1,55}', schema):
            raise ValueError('dedicated_print_schema_required')
        if connect is None:
            from core.db import get_conn
            connect = get_conn
        self.schema, self.connect = schema, connect

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(self.schema)))
                yield cursor

    def install(self):
        """Explicit setup; callers must authorize deploying the queue first."""
        with self.transaction() as cursor:
            cursor.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            cursor.execute(Path(__file__).with_name('queue_schema.sql').read_text())

    def sync_snapshot(self, rows, *, started_at, complete):
        day = moscow_day(started_at)
        if complete is not True:
            raise PrintBlocked('incomplete_sync_must_not_replace_snapshot')
        ids, natural, prepared = set(), set(), []
        for row in rows:
            raw, account = row['raw'], row['account']
            oid = str(UUID(raw['id']))
            external = raw.get('name')
            if (account not in ACCOUNTS or not isinstance(external, str) or not external
                    or not row.get('counter_key') or not selected_order(raw, account, started_at)):
                raise PrintBlocked('order_outside_print_scope')
            if oid in ids or (account, external) in natural:
                raise PrintBlocked('ambiguous_print_order')
            ids.add(oid); natural.add((account, external))
            did = str(UUID(row['demand_id'])) if row.get('demand_id') else None
            prepared.append((oid, account, external, did, row['counter_key'],
                             row.get('source_error') or (None if did else 'linked_demand_missing'), row.get('display', {})))
        sid = str(uuid4())
        with self.transaction() as cursor:
            # Serialize sync publication and counter reservation; API fetching
            # happens outside this short transaction.
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            previous = cursor.fetchone()['snapshot_id']
            if previous:
                cursor.execute('SELECT started_at FROM snapshots WHERE id=%s', (previous,))
                if cursor.fetchone()['started_at'] >= started_at:
                    return str(previous)
            cursor.execute('INSERT INTO snapshots VALUES (%s,%s,%s,%s)',
                           (sid, day, started_at, Json([r['raw'] for r in rows])))
            for oid, account, external, did, counter, error, display in prepared:
                cursor.execute('''INSERT INTO orders(id,account,external_id,demand_id,counter_key,snapshot_id,source_error,display)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
                    demand_id=EXCLUDED.demand_id,counter_key=EXCLUDED.counter_key,
                    snapshot_id=EXCLUDED.snapshot_id,source_error=EXCLUDED.source_error,display=EXCLUDED.display
                    WHERE orders.account=EXCLUDED.account AND orders.external_id=EXCLUDED.external_id''',
                    (oid, account, external, did, counter, sid, error, Json(display)))
                if cursor.rowcount != 1:
                    raise PrintBlocked('marketplace_order_identity_changed')
            cursor.execute('UPDATE current_snapshot SET snapshot_id=%s', (sid,))
        return sid

    @staticmethod
    def _batch(cursor, batch_id):
        cursor.execute('SELECT * FROM batches WHERE id=%s', (batch_id,))
        row = cursor.fetchone()
        if row is None:
            raise PrintBlocked('print_batch_missing')
        result = dict(row)
        cursor.execute('SELECT identity FROM members WHERE batch_id=%s ORDER BY ordinal', (batch_id,))
        result['orders'] = [r['identity'] for r in cursor.fetchall()]
        return result

    def batch(self, batch_id):
        with self.transaction() as cursor:
            return self._batch(cursor, batch_id)

    def order(self, order_id):
        with self.transaction() as cursor:
            cursor.execute('''SELECT o.*,c.batch_id,b.state AS batch_state,
                (e.order_id IS NOT NULL) AS printed_externally FROM orders o
                LEFT JOIN claims c ON c.order_id=o.id LEFT JOIN batches b ON b.id=c.batch_id
                LEFT JOIN external_prints e ON e.order_id=o.id
                WHERE o.id=%s''', (str(UUID(str(order_id))),))
            result = cursor.fetchone()
            if result is None: raise PrintBlocked('print_order_missing')
            return dict(result)

    def mark_external_printed(self, order_ids, *, accounts, now, evidence):
        """Record explicit user confirmation; serialize with sync and counter clicks."""
        selected = [str(UUID(str(oid))) for oid in order_ids]
        if not selected or len(selected) != len(set(selected)) or not evidence.get('sha256'):
            raise ValueError('external_print_confirmation_required')
        day = moscow_day(now)
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            cursor.fetchone()
            cursor.execute('''SELECT o.id,o.account,o.snapshot_id,s.day,b.state AS batch_state,
                e.order_id AS already_external FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                LEFT JOIN claims c ON c.order_id=o.id LEFT JOIN batches b ON b.id=c.batch_id
                LEFT JOIN external_prints e ON e.order_id=o.id
                WHERE o.id=ANY(%s::uuid[]) ORDER BY o.id FOR UPDATE OF o''', (selected,))
            rows = cursor.fetchall()
            if len(rows) != len(selected) or any(r['account'] not in accounts for r in rows):
                raise PrintBlocked('selected_orders_changed')
            if any(r['batch_state'] == 'building' for r in rows):
                raise PrintBlocked('counter_already_reserved')
            pending = [r for r in rows if r['batch_state'] != 'ready' and not r['already_external']]
            if any(r['day'] != day for r in pending):
                raise PrintBlocked('selected_orders_changed')
            for row in pending:
                cursor.execute('INSERT INTO external_prints VALUES (%s,%s,%s,%s)',
                               (row['id'], day, now, Json(evidence)))
            return dict(marked=[str(r['id']) for r in pending], already_printed=len(rows)-len(pending))

    def reserve_counter(self, counter_key, *, request_key, now, order_ids=None):
        day = moscow_day(now)
        selected = None if order_ids is None else [str(UUID(str(value))) for value in order_ids]
        if selected is not None and (not selected or len(selected) != len(set(selected))):
            raise ValueError('selected_order_ids_invalid')
        if not isinstance(request_key, str) or not request_key or len(request_key) > 100:
            raise ValueError('print_request_key_required')
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            snapshot = cursor.fetchone()['snapshot_id']
            cursor.execute('SELECT batch_id FROM requests WHERE request_key=%s', (request_key,))
            previous = cursor.fetchone()
            if previous:
                result = self._batch(cursor, previous['batch_id'])
                if result['counter_key'] != counter_key:
                    raise PrintBlocked('request_key_reused_for_other_counter')
                requested = result['display'].get('requested_order_ids', [m['ms_order_id'] for m in result['orders']])
                if selected is not None and set(requested) != set(selected):
                    raise PrintBlocked('request_key_reused_for_other_orders')
                return result
            cursor.execute("SELECT id FROM batches WHERE counter_key=%s AND day=%s AND state='building'", (counter_key, day))
            active = cursor.fetchone()
            if active:
                bid = str(active['id'])
                if selected is not None:
                    active_batch = self._batch(cursor, bid)
                    if {m['ms_order_id'] for m in active_batch['orders']} != set(selected):
                        raise PrintBlocked('counter_already_reserved')
            else:
                cursor.execute('''SELECT o.* FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                    WHERE o.snapshot_id=%s AND s.day=%s AND o.counter_key=%s
                    AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.order_id=o.id)
                    AND NOT EXISTS (SELECT 1 FROM external_prints e WHERE e.order_id=o.id)
                    AND (%s::uuid[] IS NULL OR o.id=ANY(%s::uuid[]))
                    ORDER BY o.external_id,o.id FOR UPDATE OF o''', (snapshot, day, counter_key, selected, selected))
                orders = cursor.fetchall()
                if selected is None:
                    failed = self._label_failures(self._label_requests(cursor, day)) | self._failed_orders(self._current_jobs(cursor, day))
                    orders = [o for o in orders if o['demand_id'] and not o['source_error'] and not failed.get(str(o['id']))]
                if selected is not None and len(orders) != len(selected):
                    raise PrintBlocked('selected_orders_changed')
                if not orders:
                    raise PrintBlocked('counter_empty')
                bid = str(uuid4())
                display = dict(orders[0]['display'])
                display['blocked_orders'] = {str(o['id']): o['source_error'] or 'linked_demand_missing'
                                             for o in orders if o['source_error'] or not o['demand_id']}
                cursor.execute("INSERT INTO batches(id,counter_key,day,state,display) VALUES (%s,%s,%s,'building',%s)",
                               (bid, counter_key, day, Json(display)))
                for ordinal, order in enumerate(orders):
                    identity = dict(account=order['account'], external_id=order['external_id'],
                                    ms_order_id=str(order['id']), ms_demand_id=str(order['demand_id']) if order['demand_id'] else None)
                    cursor.execute('INSERT INTO members VALUES (%s,%s,%s,%s)', (bid, order['id'], ordinal, Json(identity)))
                    cursor.execute('INSERT INTO claims VALUES (%s,%s)', (order['id'], bid))
            cursor.execute('INSERT INTO requests VALUES (%s,%s)', (request_key, bid))
            return self._batch(cursor, bid)

    def reserve_orders(self, order_ids, *, account, request_key, now, title):
        """Freeze an explicitly requested file/list across one account's counters."""
        selected = [str(UUID(str(oid))) for oid in order_ids]
        if not selected or len(set(selected)) != len(selected) or account not in ACCOUNTS:
            raise ValueError('invalid_print_selection')
        if not isinstance(request_key, str) or not request_key or len(request_key) > 100:
            raise ValueError('print_request_key_required')
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ValueError('print_selection_title_required')
        day = moscow_day(now)
        key = account + '|selection|' + sha256(json.dumps(sorted(selected)).encode()).hexdigest()
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            snapshot = cursor.fetchone()['snapshot_id']
            cursor.execute('SELECT batch_id FROM requests WHERE request_key=%s', (request_key,))
            previous = cursor.fetchone()
            if previous:
                result = self._batch(cursor, previous['batch_id'])
                if result['counter_key'] != key or set(result['display'].get('requested_order_ids', [])) != set(selected):
                    raise PrintBlocked('request_key_reused_for_other_orders')
                return result
            cursor.execute('''SELECT o.* FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                WHERE o.snapshot_id=%s AND s.day=%s AND o.id=ANY(%s::uuid[])
                ORDER BY array_position(%s::uuid[],o.id) FOR UPDATE OF o''', (snapshot, day, selected, selected))
            orders = cursor.fetchall()
            if len(orders) != len(selected) or any(o['account'] != account for o in orders):
                raise PrintBlocked('selected_orders_changed')
            cursor.execute('SELECT 1 FROM claims WHERE order_id=ANY(%s::uuid[]) LIMIT 1', (selected,))
            if cursor.fetchone(): raise PrintBlocked('selected_orders_changed')
            cursor.execute('SELECT 1 FROM external_prints WHERE order_id=ANY(%s::uuid[]) LIMIT 1', (selected,))
            if cursor.fetchone(): raise PrintBlocked('selected_orders_changed')
            cursor.execute("SELECT 1 FROM batches WHERE state='building' AND day=%s AND counter_key=ANY(%s) LIMIT 1",
                           (day, list({o['counter_key'] for o in orders} | {key})))
            if cursor.fetchone(): raise PrintBlocked('counter_already_reserved')
            display = dict(orders[0]['display'])
            display.update(direction=title, direction_id='selection', color=None,
                           requested_order_ids=selected,
                           blocked_orders={str(o['id']): o['source_error'] or 'linked_demand_missing'
                                           for o in orders if o['source_error'] or not o['demand_id']})
            if len({o['display'].get('store_id') for o in orders}) > 1:
                display.update(store='Несколько складов МС', store_id='selection')
            bid = str(uuid4())
            cursor.execute("INSERT INTO batches(id,counter_key,day,state,display) VALUES (%s,%s,%s,'building',%s)",
                           (bid, key, day, Json(display)))
            for ordinal, order in enumerate(orders):
                identity = dict(account=account, external_id=order['external_id'], ms_order_id=str(order['id']),
                                ms_demand_id=str(order['demand_id']) if order['demand_id'] else None)
                cursor.execute('INSERT INTO members VALUES (%s,%s,%s,%s)', (bid, order['id'], ordinal, Json(identity)))
                cursor.execute('INSERT INTO claims VALUES (%s,%s)', (order['id'], bid))
            cursor.execute('INSERT INTO requests VALUES (%s,%s)', (request_key, bid))
            return self._batch(cursor, bid)

    def label_candidates(self, *, now):
        with self.transaction() as cursor:
            cursor.execute('''SELECT o.*,a.label_path,a.label_sha,a.demand_id AS artifact_demand_id,
                EXISTS (SELECT 1 FROM members m JOIN batches b ON b.id=m.batch_id
                    WHERE m.order_id=o.id AND m.identity->>'ms_demand_id'=o.demand_id::text
                    AND b.state='failed' AND b.error=ANY(%s)
                    AND NOT (COALESCE(b.display->'resolved_orders','{}'::jsonb) ? o.id::text)) AS label_retry_needed
                FROM current_snapshot v JOIN snapshots s ON s.id=v.snapshot_id
                JOIN orders o ON o.snapshot_id=s.id LEFT JOIN artifacts a ON a.order_id=o.id
                WHERE s.day=%s AND o.demand_id IS NOT NULL AND o.source_error IS NULL
                AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.order_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM external_prints e WHERE e.order_id=o.id)
                ORDER BY CASE WHEN o.display->'color'->>'key'='express' THEN 0 ELSE 1 END,
                         o.counter_key,o.external_id''', (list(LABEL_AVAILABILITY_ERRORS), moscow_day(now)))
            return [dict(row) for row in cursor.fetchall()]

    def resolve_label_errors(self, order_id, *, demand_id, now):
        """Resolve past label-availability errors after a fresh successful check."""
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            snapshot = cursor.fetchone()['snapshot_id']
            cursor.execute('''SELECT o.demand_id,a.demand_id AS artifact_demand_id,a.label_path,a.label_sha
                FROM orders o JOIN artifacts a ON a.order_id=o.id
                WHERE o.id=%s AND o.snapshot_id=%s FOR UPDATE OF o''', (order_id, snapshot))
            row = cursor.fetchone()
            if not row or str(row['demand_id']) != str(demand_id) or str(row['artifact_demand_id']) != str(demand_id):
                return 0
            cursor.execute('SELECT 1 FROM claims WHERE order_id=%s', (order_id,))
            if cursor.fetchone() or not row['label_path']: return 0
            checked_file(row['label_path'], row['label_sha'])
            marker = Json({str(order_id): {'kind':'label', 'at':now.isoformat()}})
            cursor.execute('''UPDATE batches b SET display=jsonb_set(b.display,'{resolved_orders}',
                    COALESCE(b.display->'resolved_orders','{}'::jsonb) || %s)
                WHERE b.state='failed' AND b.error=ANY(%s) AND b.created_at<=%s
                AND (b.display->>'error_order_id' IS NULL OR b.display->>'error_order_id'=%s)
                AND EXISTS (SELECT 1 FROM members m WHERE m.batch_id=b.id AND m.order_id=%s
                    AND m.identity->>'ms_demand_id'=%s)''',
                (marker, list(LABEL_AVAILABILITY_ERRORS), now, str(order_id), order_id, str(demand_id)))
            count = cursor.rowcount
            cursor.execute("""UPDATE label_requests SET resolved_at=%s WHERE order_id=%s
                AND demand_id=%s AND state='failed' AND finished_at<=%s AND resolved_at IS NULL""",
                (now, order_id, demand_id, now))
            return count + cursor.rowcount

    def enqueue_label_request(self, order_id, *, request_key, now):
        oid = str(UUID(str(order_id)))
        if not isinstance(request_key, str) or not request_key or len(request_key)>100:
            raise ValueError('label_request_key_required')
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            snapshot = cursor.fetchone()['snapshot_id']
            cursor.execute('SELECT * FROM label_requests WHERE request_key=%s', (request_key,))
            previous = cursor.fetchone()
            if previous:
                if str(previous['order_id']) != oid: raise PrintBlocked('request_key_reused_for_other_orders')
                return dict(previous)
            cursor.execute('''SELECT o.* FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                WHERE o.id=%s AND o.snapshot_id=%s AND s.day=%s FOR UPDATE OF o''',
                (oid, snapshot, moscow_day(now)))
            order = cursor.fetchone()
            if not order: raise PrintBlocked('order_outside_print_scope')
            if not order['demand_id']: raise PrintBlocked('linked_demand_missing')
            cursor.execute('SELECT 1 FROM claims WHERE order_id=%s UNION ALL SELECT 1 FROM external_prints WHERE order_id=%s', (oid, oid))
            if cursor.fetchone(): raise PrintBlocked('counter_already_reserved')
            cursor.execute("SELECT * FROM label_requests WHERE order_id=%s AND state IN ('pending','running')", (oid,))
            active = cursor.fetchone()
            if active: return dict(active)
            cursor.execute("""INSERT INTO label_requests(id,request_key,order_id,demand_id,day,state,created_at)
                VALUES(%s,%s,%s,%s,%s,'pending',%s) RETURNING *""",
                (str(uuid4()), request_key, oid, order['demand_id'], moscow_day(now), now))
            return dict(cursor.fetchone())

    def start_label_request(self, *, now):
        with self.transaction() as cursor:
            cursor.execute("""UPDATE label_requests SET state='failed',finished_at=%s,error='order_outside_print_scope'
                WHERE state='pending' AND day<%s""", (now, moscow_day(now)))
            cursor.execute("SELECT id FROM label_requests WHERE state='pending' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1")
            row = cursor.fetchone()
            if not row: return None
            cursor.execute("UPDATE label_requests SET state='running',started_at=%s WHERE id=%s RETURNING *", (now,row['id']))
            return dict(cursor.fetchone())

    def finish_label_request(self, request_id, *, now, error=None):
        with self.transaction() as cursor:
            cursor.execute("""UPDATE label_requests SET state=%s,finished_at=%s,error=%s
                WHERE id=%s AND state='running'""", ('failed' if error else 'ready', now, error, request_id))

    @staticmethod
    def _label_requests(cursor, day):
        cursor.execute('''SELECT DISTINCT ON (r.order_id) r.* FROM label_requests r
            JOIN orders o ON o.id=r.order_id JOIN current_snapshot v ON v.snapshot_id=o.snapshot_id
            WHERE r.day=%s AND r.demand_id=o.demand_id ORDER BY r.order_id,r.created_at DESC,r.id DESC''', (day,))
        return {str(row['order_id']): dict(row) for row in cursor.fetchall()}

    @staticmethod
    def _label_failures(requests):
        return {oid: row['error'] for oid,row in requests.items()
                if row['state']=='failed' and not row['resolved_at']}

    def mark_prefetched_label(self, order_id, *, demand_id, path, now):
        path, digest = checked_file(path)
        with self.transaction() as cursor:
            # Recheck scope and claims under the same snapshot lock as a
            # counter reservation. Background work must never replace files
            # belonging to an in-flight/published pair.
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            snapshot = cursor.fetchone()['snapshot_id']
            cursor.execute('''SELECT o.demand_id FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                WHERE o.id=%s AND o.snapshot_id=%s AND s.day=%s FOR UPDATE OF o''',
                (order_id, snapshot, moscow_day(now)))
            order = cursor.fetchone()
            if not order or str(order['demand_id']) != str(demand_id): return False
            cursor.execute('SELECT 1 FROM claims WHERE order_id=%s', (order_id,))
            if cursor.fetchone(): return False
            cursor.execute('SELECT 1 FROM external_prints WHERE order_id=%s', (order_id,))
            if cursor.fetchone(): return False
            cursor.execute('''SELECT demand_id,label_path,label_sha FROM artifacts WHERE order_id=%s''', (order_id,))
            existing = cursor.fetchone()
            if existing and str(existing['demand_id']) == str(demand_id) and existing['label_path'] == path and existing['label_sha'] == digest:
                return True
            cursor.execute('''INSERT INTO artifacts(order_id,demand_id,source_key,label_path,label_sha)
                VALUES (%s,%s,'',%s,%s) ON CONFLICT(order_id) DO UPDATE SET
                demand_id=EXCLUDED.demand_id,source_key='',label_path=EXCLUDED.label_path,
                label_sha=EXCLUDED.label_sha,receipt_path=NULL,receipt_sha=NULL''',
                (order_id, demand_id, path, digest))
            return True

    def mark_artifact(self, order_id, *, demand_id, kind, path, source_key=''):
        if kind not in ('receipt', 'label'):
            raise ValueError('unknown_print_artifact')
        path, digest = checked_file(path)
        with self.transaction() as cursor:
            cursor.execute('SELECT demand_id FROM orders WHERE id=%s FOR UPDATE', (order_id,))
            order = cursor.fetchone()
            if not order or str(order['demand_id']) != str(demand_id):
                raise PrintBlocked('artifact_demand_identity_changed')
            cursor.execute('''INSERT INTO artifacts(order_id,demand_id,source_key) VALUES (%s,%s,%s)
                ON CONFLICT(order_id) DO UPDATE SET demand_id=EXCLUDED.demand_id,source_key=EXCLUDED.source_key,
                receipt_path=CASE WHEN artifacts.demand_id=EXCLUDED.demand_id AND artifacts.source_key=EXCLUDED.source_key THEN artifacts.receipt_path END,
                receipt_sha=CASE WHEN artifacts.demand_id=EXCLUDED.demand_id AND artifacts.source_key=EXCLUDED.source_key THEN artifacts.receipt_sha END,
                label_path=CASE WHEN artifacts.demand_id=EXCLUDED.demand_id AND artifacts.source_key=EXCLUDED.source_key THEN artifacts.label_path END,
                label_sha=CASE WHEN artifacts.demand_id=EXCLUDED.demand_id AND artifacts.source_key=EXCLUDED.source_key THEN artifacts.label_sha END''', (order_id, demand_id, source_key))
            cursor.execute(sql.SQL('UPDATE artifacts SET {}=%s,{}=%s WHERE order_id=%s').format(
                sql.Identifier(kind + '_path'), sql.Identifier(kind + '_sha')), (path, digest, order_id))

    @staticmethod
    def _check_pair(path, expected):
        path = Path(path).resolve()
        try:
            manifest = json.loads((path / 'manifest.json').read_text())
        except (OSError, ValueError):
            raise PrintBlocked('pair_manifest_unavailable') from None
        actual = [{k: row.get(k) for k in ('account','external_id','ms_order_id','ms_demand_id')}
                  for row in manifest.get('orders', [])]
        if actual != expected:
            raise PrintBlocked('pair_order_sequence_mismatch')
        for name in ('receipts.pdf', 'labels.pdf'):
            digest = manifest.get('output_sha256', {}).get(name)
            if not digest:
                raise PrintBlocked('both_pdfs_required')
            checked_file(path / name, digest)
        return str(path), manifest

    def publish_pair(self, batch_id, *, generation, pair_dir):
        expected = self.batch(batch_id)['orders']
        path, manifest = self._check_pair(pair_dir, expected)
        with self.transaction() as cursor:
            cursor.execute('SELECT state,generation,pair_dir,manifest,display FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
            batch = cursor.fetchone()
            if batch['state'] == 'ready':
                if batch['pair_dir'] == path and batch['manifest'] == manifest:
                    return
                raise PrintBlocked('batch_already_published')
            if batch['state'] != 'building' or batch['generation'] != generation:
                raise PrintBlocked('stale_print_worker')
            cursor.execute('''SELECT o.id,o.demand_id AS current_demand_id,a.*,c.batch_id FROM members m
                JOIN orders o ON o.id=m.order_id LEFT JOIN artifacts a ON a.order_id=o.id
                LEFT JOIN claims c ON c.order_id=o.id WHERE m.batch_id=%s ORDER BY m.ordinal
                FOR UPDATE OF o''', (batch_id,))
            records = cursor.fetchall()
            for row, identity, output in zip(records, expected, manifest['orders']):
                if (str(row['batch_id']) != str(batch_id) or str(row['current_demand_id']) != identity['ms_demand_id']
                        or str(row['demand_id']) != identity['ms_demand_id']
                        or not row['receipt_path'] or not row['label_path']
                        or output.get('receipts') != [row['receipt_path']] or output.get('labels') != [row['label_path']]):
                    raise PrintBlocked('pair_artifacts_or_claims_changed')
                checked_file(row['receipt_path'], row['receipt_sha'])
                checked_file(row['label_path'], row['label_sha'])
            if len(records) != len(expected):
                raise PrintBlocked('print_batch_incomplete')
            display=dict(batch['display']);included={m['ms_order_id'] for m in expected}
            previous=[e for e in display.get('excluded_orders',[]) if e['ms_order_id'] in included]
            if previous:
                display['past_exclusions']=display.get('past_exclusions',[])+previous
                display['excluded_orders']=[e for e in display['excluded_orders'] if e['ms_order_id'] not in included]
            cursor.execute("""UPDATE batches SET state='ready',pair_dir=%s,manifest=%s,published_at=now(),error=NULL,display=%s
                WHERE id=%s""", (path, Json(manifest), Json(display), batch_id))

    def fail_batch(self, batch_id, *, generation, error, order_id=None):
        with self.transaction() as cursor:
            cursor.execute('SELECT state,generation FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
            batch = cursor.fetchone()
            if not batch or batch['state'] != 'building' or batch['generation'] != generation:
                raise PrintBlocked('stale_print_worker')
            # No ready list ever exposed these files. Keep per-order artifacts
            # for retry; release only the unpublished reservation.
            context = {}
            if order_id is not None:
                order_id = str(UUID(str(order_id)))
                cursor.execute('SELECT 1 FROM members WHERE batch_id=%s AND order_id=%s', (batch_id, order_id))
                if not cursor.fetchone(): raise PrintBlocked('error_order_not_in_batch')
                context['error_order_id'] = order_id
            cursor.execute("""UPDATE batches SET state='failed',error=%s,
                display=(display-'error_order_id'-'resolved_orders') || %s WHERE id=%s""", (str(error)[:200], Json(context), batch_id))
            cursor.execute('DELETE FROM claims WHERE batch_id=%s', (batch_id,))

    def exclude_failed_orders(self, batch_id, *, generation, failures):
        """Keep successful members in the requested pair; retain each failure.

        Publication still checks one exact frozen sequence. Failed members move
        to failed child jobs and become available to the counter atomically.
        If none succeeded, the original job retains one failure for its caller.
        """
        errors = {str(UUID(str(f['ms_order_id']))): str(f['error'])[:200] for f in failures}
        if not errors or len(errors) != len(failures): raise ValueError('invalid_failed_print_orders')
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            cursor.execute('SELECT * FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
            batch = cursor.fetchone()
            if not batch or batch['state'] != 'building' or batch['generation'] != generation:
                raise PrintBlocked('stale_print_worker')
            cursor.execute('''SELECT m.*,c.batch_id AS claim_batch_id FROM members m
                LEFT JOIN claims c ON c.order_id=m.order_id WHERE m.batch_id=%s ORDER BY m.ordinal''', (batch_id,))
            members = cursor.fetchall()
            if not set(errors) <= {str(m['order_id']) for m in members}:
                raise PrintBlocked('error_order_not_in_batch')
            if any(str(m['claim_batch_id']) != str(batch_id) for m in members):
                raise PrintBlocked('pair_artifacts_or_claims_changed')
            all_failed = len(errors) == len(members)
            anchor = str(members[0]['order_id']) if all_failed else None
            display = dict(batch['display'])
            display.pop('resolved_orders', None)
            display.setdefault('requested_order_ids', [str(m['order_id']) for m in members])
            excluded = {e['ms_order_id']: e for e in display.get('excluded_orders', [])}
            for member in members:
                oid = str(member['order_id'])
                if oid not in errors: continue
                identity = member['identity']
                excluded[oid] = dict(identity, error=errors[oid])
                cursor.execute('DELETE FROM claims WHERE order_id=%s AND batch_id=%s', (oid, batch_id))
                if oid == anchor: continue
                child = str(uuid4())
                child_display = {k:v for k,v in batch['display'].items()
                                 if k not in ('requested_order_ids','excluded_orders','blocked_orders','error_order_id','resolved_orders')}
                child_display.update(parent_batch_id=str(batch_id), error_order_id=oid)
                cursor.execute('''INSERT INTO batches(id,counter_key,day,state,display,error)
                    VALUES (%s,%s,%s,'failed',%s,%s)''',
                    (child, batch['counter_key'], batch['day'], Json(child_display), errors[oid]))
                cursor.execute('INSERT INTO members VALUES (%s,%s,0,%s)', (child, oid, Json(identity)))
                cursor.execute('DELETE FROM members WHERE batch_id=%s AND order_id=%s', (batch_id, oid))
            display['excluded_orders'] = list(excluded.values())
            if all_failed:
                display['error_order_id'] = anchor
                cursor.execute("UPDATE batches SET state='failed',error=%s,display=%s WHERE id=%s",
                               (errors[anchor], Json(display), batch_id))
            else:
                display.pop('error_order_id', None)
                cursor.execute('UPDATE batches SET display=%s WHERE id=%s', (Json(display), batch_id))
            return dict(state='failed' if all_failed else 'building', excluded=len(errors))

    def recover_batch(self, batch_id, *, generation):
        with self.transaction() as cursor:
            cursor.execute("""UPDATE batches SET generation=generation+1 WHERE id=%s
                AND state='building' AND generation=%s RETURNING generation""", (batch_id, generation))
            result = cursor.fetchone()
            if not result:
                raise PrintBlocked('stale_print_worker')
            return result['generation']

    def retry_failed_batch(self, batch_id, *, generation):
        """Resume an already requested unpublished pair with its frozen members."""
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            cursor.execute('SELECT state,generation,counter_key,day FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
            batch = cursor.fetchone()
            if not batch or batch['state'] != 'failed' or batch['generation'] != generation:
                raise PrintBlocked('stale_print_worker')
            cursor.execute("SELECT 1 FROM batches WHERE counter_key=%s AND day=%s AND state='building'",
                           (batch['counter_key'], batch['day']))
            if cursor.fetchone(): raise PrintBlocked('counter_already_reserved')
            cursor.execute('SELECT count(*) AS count FROM members WHERE batch_id=%s', (batch_id,))
            if not cursor.fetchone()['count']: raise PrintBlocked('print_batch_empty')
            cursor.execute('''SELECT 1 FROM members m JOIN claims c ON c.order_id=m.order_id
                              WHERE m.batch_id=%s LIMIT 1''', (batch_id,))
            if cursor.fetchone(): raise PrintBlocked('counter_already_reserved')
            cursor.execute('''SELECT 1 FROM members m JOIN external_prints e ON e.order_id=m.order_id
                              WHERE m.batch_id=%s LIMIT 1''', (batch_id,))
            if cursor.fetchone(): raise PrintBlocked('selected_orders_changed')
            cursor.execute('INSERT INTO claims SELECT order_id,batch_id FROM members WHERE batch_id=%s', (batch_id,))
            cursor.execute("UPDATE batches SET state='building',generation=generation+1,error=NULL WHERE id=%s", (batch_id,))
            return self._batch(cursor, batch_id)

    def ready_files(self, batch_id):
        batch = self.batch(batch_id)
        if batch['state'] != 'ready':
            raise PrintBlocked('pair_not_ready')
        path, manifest = self._check_pair(batch['pair_dir'], batch['orders'])
        if manifest != batch['manifest']:
            raise PrintBlocked('published_manifest_changed')
        return dict(receipts=Path(path) / 'receipts.pdf', labels=Path(path) / 'labels.pdf')

    @staticmethod
    def _current_jobs(cursor, day):
        cursor.execute("""SELECT b.id,b.counter_key,b.state,b.error,b.created_at,
            COALESCE(problem.orders,'[]'::jsonb) AS orders FROM batches b
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object('ms_order_id',m.order_id::text,
                    'external_id',m.identity->>'external_id','account',m.identity->>'account')
                    ORDER BY m.ordinal) AS orders
                FROM members m JOIN orders o ON o.id=m.order_id
                JOIN current_snapshot v ON v.snapshot_id=o.snapshot_id
                JOIN snapshots s ON s.id=v.snapshot_id
                LEFT JOIN claims c ON c.order_id=o.id
                WHERE m.batch_id=b.id AND b.state='failed' AND b.day=%s AND s.day=%s
                AND m.identity->>'ms_demand_id'=o.demand_id::text
                AND c.batch_id IS NULL
                AND NOT EXISTS (SELECT 1 FROM external_prints e WHERE e.order_id=o.id)
                AND NOT (COALESCE(b.display->'resolved_orders','{}'::jsonb) ? m.order_id::text)
                AND (b.display->>'error_order_id' IS NULL
                    OR b.display->>'error_order_id'=m.order_id::text)
            ) problem ON true
            WHERE (b.state='building' AND b.day=%s) OR (b.state='failed' AND problem.orders IS NOT NULL)
            ORDER BY b.created_at DESC,b.id DESC""", (day, day, day))
        jobs = [dict(r) for r in cursor.fetchall()]
        seen = set()
        for job in jobs:
            if job['state'] == 'failed':
                job['orders'] = [order for order in job['orders'] if order['ms_order_id'] not in seen]
                seen.update(order['ms_order_id'] for order in job['orders'])
        jobs = [job for job in jobs if job['state'] != 'failed' or job['orders']]
        return jobs

    @staticmethod
    def _failed_orders(jobs):
        return {order['ms_order_id']: job['error'] for job in reversed(jobs) if job['state']=='failed'
                for order in job.get('orders', [])}

    def view(self, *, now):
        day = moscow_day(now)
        with self.transaction() as cursor:
            cursor.execute('''SELECT o.*,b.state AS batch_state,c.batch_id,a.receipt_path,a.receipt_sha,
                a.label_path,a.label_sha,a.demand_id AS artifact_demand_id,
                (e.order_id IS NOT NULL) AS printed_externally
                FROM current_snapshot v JOIN snapshots s ON s.id=v.snapshot_id
                JOIN orders o ON o.snapshot_id=s.id LEFT JOIN claims c ON c.order_id=o.id
                LEFT JOIN batches b ON b.id=c.batch_id LEFT JOIN artifacts a ON a.order_id=o.id
                LEFT JOIN external_prints e ON e.order_id=o.id
                WHERE s.day=%s ORDER BY o.counter_key,o.external_id,o.id''', (day,))
            rows = [dict(r) for r in cursor.fetchall()]
            cursor.execute("""SELECT b.id,b.counter_key,b.day,b.published_at,b.display,
                (SELECT count(*) FROM members m WHERE m.batch_id=b.id) AS order_count
                FROM batches b WHERE b.state='ready' AND b.day=%s ORDER BY b.published_at DESC""", (day,))
            ready = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT s.started_at,s.day FROM current_snapshot v LEFT JOIN snapshots s ON s.id=v.snapshot_id')
            snapshot = cursor.fetchone()
            cursor.execute('SELECT * FROM sync_runs ORDER BY created_at DESC LIMIT 1')
            sync = cursor.fetchone()
            jobs = self._current_jobs(cursor, day)
            refreshes = self._label_requests(cursor, day)
            cursor.execute('SELECT last_seen FROM worker_status WHERE singleton=true')
            worker = cursor.fetchone()
            external_counts = self._external_printed_counts(cursor, day)
        counters = {}
        failed = self._failed_orders(jobs)
        label_failures = self._label_failures(refreshes)
        for row in rows:
            row['printed'] = row['batch_state'] == 'ready' or row['printed_externally']
            row['color'] = 'green' if row['printed'] else 'red'
            row['preparation_error'] = failed.get(str(row['id'])) if row['batch_state'] != 'building' else None
            row['label_refresh'] = refreshes.get(str(row['id']))
            row['label_error'] = label_failures.get(str(row['id'])) if not row['printed'] and row['batch_state'] != 'building' else None
            row['counter_eligible'] = not row['printed'] and bool(row['demand_id']) and not row['source_error'] and not row['preparation_error'] and not row['label_error']
            for kind in ('receipt', 'label'):
                try:
                    if row['demand_id'] != row['artifact_demand_id'] or not row[kind + '_path']:
                        raise PrintBlocked('artifact_not_received')
                    checked_file(row[kind + '_path'], row[kind + '_sha'])
                    row[kind + '_ready'] = True
                except PrintBlocked:
                    row[kind + '_ready'] = False
            counter = counters.setdefault(row['counter_key'], dict(count=0, building=False, blocked=0))
            counter['building'] |= row['batch_state'] == 'building'
            if not row['printed']:
                counter['count'] += bool(row['counter_eligible'])
                counter['blocked'] += not row['counter_eligible']
        return dict(rows=rows, counters=counters, ready_batches=ready, external_printed_counts=external_counts,
                    jobs=jobs, last_sync=snapshot['started_at'], snapshot_day=snapshot['day'], sync=dict(sync) if sync else None,
                    worker_healthy=bool(worker and 0 <= (now - worker['last_seen']).total_seconds() < 90))

    @staticmethod
    def _external_printed_counts(cursor, day, accounts=None):
        cursor.execute('''SELECT o.account,count(*) AS count FROM external_prints e
            JOIN orders o ON o.id=e.order_id WHERE e.day=%s AND o.account=ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM claims c JOIN batches b ON b.id=c.batch_id
                            WHERE c.order_id=o.id AND b.state='ready') GROUP BY o.account''',
            (day, list(ACCOUNTS if accounts is None else accounts)))
        return {row['account']: row['count'] for row in cursor.fetchall()}

    def archive(self, *, now, day=None, accounts=None):
        """Read past business days without touching claims, exports or PDF files.

        Orders can leave the live MS filter or move to another shipment day.
        Recover their last observation *on each day* from immutable raw snapshots,
        rather than treating the mutable orders table as historical state.
        """
        today = moscow_day(now)
        if day is not None and day >= today:
            raise ValueError('archive_requires_past_day')
        allowed = list(ACCOUNTS if accounts is None else accounts)
        with self.transaction() as cursor:
            cursor.execute('''WITH seen AS (
                SELECT s.day,o.id FROM snapshots s
                CROSS JOIN LATERAL jsonb_array_elements(s.raw_orders) r(raw)
                JOIN orders o ON o.id=(r.raw->>'id')::uuid
                WHERE s.day<%s AND o.account=ANY(%s)
                UNION
                SELECT b.day,m.order_id FROM batches b JOIN members m ON m.batch_id=b.id
                JOIN orders o ON o.id=m.order_id
                WHERE b.day<%s AND b.state='ready' AND o.account=ANY(%s)
                UNION
                SELECT e.day,e.order_id FROM external_prints e JOIN orders o ON o.id=e.order_id
                WHERE e.day<%s AND o.account=ANY(%s)
            ) SELECT seen.day,count(*) AS order_count,
                count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM members m JOIN batches b ON b.id=m.batch_id
                    WHERE m.order_id=seen.id AND b.day=seen.day AND b.state='ready'
                ) OR EXISTS (SELECT 1 FROM external_prints e
                             WHERE e.order_id=seen.id AND e.day=seen.day)) AS printed_count
                FROM seen GROUP BY seen.day ORDER BY seen.day DESC''',
                (today, allowed, today, allowed, today, allowed))
            days = [dict(row) for row in cursor.fetchall()]
            if day is None:
                day = days[0]['day'] if days else None
            result = dict(day=day, days=days, rows=[], counters={}, ready_batches=[], jobs=[],
                          sync=None, last_sync=None, worker_healthy=False, external_printed_counts={})
            if day is None:
                return result
            result['external_printed_counts'] = self._external_printed_counts(cursor, day, accounts)
            cursor.execute('''SELECT b.id,b.counter_key,b.day,b.published_at,b.display,
                (SELECT count(*) FROM members m WHERE m.batch_id=b.id) AS order_count
                FROM batches b WHERE b.state='ready' AND b.day=%s
                AND EXISTS (SELECT 1 FROM members m JOIN orders o ON o.id=m.order_id
                            WHERE m.batch_id=b.id AND o.account=ANY(%s))
                AND NOT EXISTS (SELECT 1 FROM members m JOIN orders o ON o.id=m.order_id
                                WHERE m.batch_id=b.id AND NOT o.account=ANY(%s))
                ORDER BY b.published_at DESC,b.id''', (day, allowed, allowed))
            result['ready_batches'] = [dict(row) for row in cursor.fetchall()]
            cursor.execute('''WITH observed AS (
                SELECT DISTINCT ON (o.id) o.id,r.raw,s.started_at
                FROM snapshots s CROSS JOIN LATERAL jsonb_array_elements(s.raw_orders) r(raw)
                JOIN orders o ON o.id=(r.raw->>'id')::uuid
                WHERE s.day=%s AND o.account=ANY(%s)
                ORDER BY o.id,s.started_at DESC,s.id DESC
            ), printed AS (
                SELECT DISTINCT ON (m.order_id) m.order_id,b.id AS batch_id,b.counter_key,b.display
                FROM batches b JOIN members m ON m.batch_id=b.id JOIN orders o ON o.id=m.order_id
                WHERE b.day=%s AND b.state='ready' AND o.account=ANY(%s)
                ORDER BY m.order_id,b.published_at DESC,b.id
            ), external AS (
                SELECT e.order_id FROM external_prints e JOIN orders o ON o.id=e.order_id
                WHERE e.day=%s AND o.account=ANY(%s)
            ) SELECT o.id,o.account,o.external_id,h.raw,h.started_at,p.batch_id,
                p.counter_key,p.display AS batch_display,(e.order_id IS NOT NULL) AS printed_externally,
                CASE WHEN s.day=%s THEN o.display ELSE '{}'::jsonb END AS observed_display
                FROM observed h FULL JOIN printed p ON p.order_id=h.id
                FULL JOIN external e ON e.order_id=COALESCE(h.id,p.order_id)
                JOIN orders o ON o.id=COALESCE(h.id,p.order_id,e.order_id)
                JOIN snapshots s ON s.id=o.snapshot_id
                ORDER BY o.account,o.external_id,o.id''', (day, allowed, day, allowed, day, allowed, day))
            for record in cursor.fetchall():
                raw = record['raw'] or {}
                display = record['observed_display'] or {}
                batch_display = record['batch_display'] or {}
                platform = {'oz': 'Ozon', 'wb': 'WB', 'ya': 'FBS Маркет'}[record['account'].split('_')[0]]
                has_files = record['batch_id'] is not None
                printed = has_files or record['printed_externally']
                result['rows'].append(dict(
                    id=record['id'], account=record['account'], external_id=record['external_id'],
                    counter_key=record['counter_key'] or record['account'] + '|archive',
                    batch_id=record['batch_id'], batch_state='ready' if has_files else None,
                    printed=printed, color='green' if printed else 'red',
                    printed_externally=record['printed_externally'],
                    receipt_ready=has_files, label_ready=has_files,
                    display=dict(ms_status=raw.get('state', {}).get('name'),
                        store=(raw.get('store') or {}).get('name') or display.get('store') or batch_display.get('store', ''),
                        direction=display.get('direction') or batch_display.get('direction') or platform)))
            cursor.execute('SELECT max(started_at) AS last_sync FROM snapshots WHERE day=%s', (day,))
            result['last_sync'] = cursor.fetchone()['last_sync']
        return result

    def heartbeat(self, now):
        with self.transaction() as cursor:
            cursor.execute('INSERT INTO worker_status VALUES(true,%s) ON CONFLICT(singleton) DO UPDATE SET last_seen=EXCLUDED.last_seen', (now,))

    def worker_healthy(self, now):
        with self.transaction() as cursor:
            cursor.execute('SELECT last_seen FROM worker_status WHERE singleton=true')
            row = cursor.fetchone()
            return bool(row and 0 <= (now - row['last_seen']).total_seconds() < 90)

    def sync_clock(self):
        with self.transaction() as cursor:
            cursor.execute('SELECT s.started_at,s.day FROM current_snapshot v LEFT JOIN snapshots s ON s.id=v.snapshot_id')
            return dict(cursor.fetchone())

    def installed(self):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT to_regclass(%s)', (self.schema + '.orders',))
                return cursor.fetchone()[0] is not None

    def enqueue_sync(self):
        with self.transaction() as cursor:
            cursor.execute('SELECT snapshot_id FROM current_snapshot FOR UPDATE')
            cursor.execute("SELECT id FROM sync_runs WHERE state IN ('pending','running')")
            active = cursor.fetchone()
            if active: return str(active['id'])
            sid = str(uuid4())
            cursor.execute("INSERT INTO sync_runs(id,state) VALUES (%s,'pending')", (sid,))
            return sid

    def start_sync(self):
        with self.transaction() as cursor:
            cursor.execute("SELECT id FROM sync_runs WHERE state='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
            row = cursor.fetchone()
            if not row: return None
            cursor.execute("UPDATE sync_runs SET state='running',started_at=now() WHERE id=%s", (row['id'],))
            return str(row['id'])

    def finish_sync(self, sync_id, *, error=None):
        with self.transaction() as cursor:
            cursor.execute('UPDATE sync_runs SET state=%s,finished_at=now(),error=%s WHERE id=%s AND state=%s',
                           ('failed' if error else 'ready', str(error)[:200] if error else None, sync_id, 'running'))

    def pending_batch(self):
        with self.transaction() as cursor:
            cursor.execute("SELECT id FROM batches WHERE state='building' ORDER BY created_at LIMIT 1")
            row = cursor.fetchone()
            return self._batch(cursor, row['id']) if row else None
