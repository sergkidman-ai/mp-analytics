# поток: print
"""Persistent counter ledger in an isolated PostgreSQL schema.

The UI term 'printed' means BOTH PDFs were published to the ready-file list,
as explicitly requested by the user. No printer confirmation or MP/MS writes.
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

    def reserve_counter(self, counter_key, *, request_key, now):
        day = moscow_day(now)
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
                return result
            cursor.execute("SELECT id FROM batches WHERE counter_key=%s AND day=%s AND state='building'", (counter_key, day))
            active = cursor.fetchone()
            if active:
                bid = str(active['id'])
            else:
                cursor.execute('''SELECT o.* FROM orders o JOIN snapshots s ON s.id=o.snapshot_id
                    WHERE o.snapshot_id=%s AND s.day=%s AND o.counter_key=%s
                    AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.order_id=o.id)
                    ORDER BY o.external_id,o.id FOR UPDATE OF o''', (snapshot, day, counter_key))
                orders = cursor.fetchall()
                if not orders:
                    raise PrintBlocked('counter_empty')
                if any(o['source_error'] or not o['demand_id'] for o in orders):
                    raise PrintBlocked('counter_has_blocked_orders')
                bid = str(uuid4())
                cursor.execute("INSERT INTO batches(id,counter_key,day,state,display) VALUES (%s,%s,%s,'building',%s)",
                               (bid, counter_key, day, Json(orders[0]['display'])))
                for ordinal, order in enumerate(orders):
                    identity = dict(account=order['account'], external_id=order['external_id'],
                                    ms_order_id=str(order['id']), ms_demand_id=str(order['demand_id']))
                    cursor.execute('INSERT INTO members VALUES (%s,%s,%s,%s)', (bid, order['id'], ordinal, Json(identity)))
                    cursor.execute('INSERT INTO claims VALUES (%s,%s)', (order['id'], bid))
            cursor.execute('INSERT INTO requests VALUES (%s,%s)', (request_key, bid))
            return self._batch(cursor, bid)

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
            cursor.execute('SELECT state,generation,pair_dir,manifest FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
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
            cursor.execute("""UPDATE batches SET state='ready',pair_dir=%s,manifest=%s,published_at=now(),error=NULL
                WHERE id=%s""", (path, Json(manifest), batch_id))

    def fail_batch(self, batch_id, *, generation, error):
        with self.transaction() as cursor:
            cursor.execute('SELECT state,generation FROM batches WHERE id=%s FOR UPDATE', (batch_id,))
            batch = cursor.fetchone()
            if not batch or batch['state'] != 'building' or batch['generation'] != generation:
                raise PrintBlocked('stale_print_worker')
            # No ready list ever exposed these files. Keep per-order artifacts
            # for retry; release only the unpublished reservation.
            cursor.execute("UPDATE batches SET state='failed',error=%s WHERE id=%s", (str(error)[:200], batch_id))
            cursor.execute('DELETE FROM claims WHERE batch_id=%s', (batch_id,))

    def recover_batch(self, batch_id, *, generation):
        with self.transaction() as cursor:
            cursor.execute("""UPDATE batches SET generation=generation+1 WHERE id=%s
                AND state='building' AND generation=%s RETURNING generation""", (batch_id, generation))
            result = cursor.fetchone()
            if not result:
                raise PrintBlocked('stale_print_worker')
            return result['generation']

    def ready_files(self, batch_id):
        batch = self.batch(batch_id)
        if batch['state'] != 'ready':
            raise PrintBlocked('pair_not_ready')
        path, manifest = self._check_pair(batch['pair_dir'], batch['orders'])
        if manifest != batch['manifest']:
            raise PrintBlocked('published_manifest_changed')
        return dict(receipts=Path(path) / 'receipts.pdf', labels=Path(path) / 'labels.pdf')

    def view(self, *, now):
        day = moscow_day(now)
        with self.transaction() as cursor:
            cursor.execute('''SELECT o.*,b.state AS batch_state,c.batch_id,a.receipt_path,a.receipt_sha,
                a.label_path,a.label_sha,a.demand_id AS artifact_demand_id
                FROM current_snapshot v JOIN snapshots s ON s.id=v.snapshot_id
                JOIN orders o ON o.snapshot_id=s.id LEFT JOIN claims c ON c.order_id=o.id
                LEFT JOIN batches b ON b.id=c.batch_id LEFT JOIN artifacts a ON a.order_id=o.id
                WHERE s.day=%s ORDER BY o.counter_key,o.external_id,o.id''', (day,))
            rows = [dict(r) for r in cursor.fetchall()]
            cursor.execute("""SELECT b.id,b.counter_key,b.day,b.published_at,b.display,
                (SELECT count(*) FROM members m WHERE m.batch_id=b.id) AS order_count
                FROM batches b WHERE b.state='ready' ORDER BY b.published_at DESC""")
            ready = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT s.started_at,s.day FROM current_snapshot v LEFT JOIN snapshots s ON s.id=v.snapshot_id')
            snapshot = cursor.fetchone()
            cursor.execute('SELECT * FROM sync_runs ORDER BY created_at DESC LIMIT 1')
            sync = cursor.fetchone()
            cursor.execute("""SELECT b.id,b.counter_key,b.state,b.error,b.created_at FROM batches b
                WHERE b.state='building' OR (b.state='failed' AND EXISTS (
                    SELECT 1 FROM members m LEFT JOIN claims c ON c.order_id=m.order_id
                    LEFT JOIN batches done ON done.id=c.batch_id WHERE m.batch_id=b.id
                    AND done.state IS DISTINCT FROM 'ready')) ORDER BY b.created_at DESC LIMIT 30""")
            jobs = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT last_seen FROM worker_status WHERE singleton=true')
            worker = cursor.fetchone()
        counters = {}
        for row in rows:
            row['printed'] = row['batch_state'] == 'ready'
            row['color'] = 'green' if row['printed'] else 'red'
            for kind in ('receipt', 'label'):
                try:
                    if row['demand_id'] != row['artifact_demand_id'] or not row[kind + '_path']:
                        raise PrintBlocked('artifact_not_received')
                    checked_file(row[kind + '_path'], row[kind + '_sha'])
                    row[kind + '_ready'] = True
                except PrintBlocked:
                    row[kind + '_ready'] = False
            counter = counters.setdefault(row['counter_key'], dict(count=0, building=False, blocked=0))
            if not row['printed']:
                counter['count'] += 1
                counter['building'] |= row['batch_state'] == 'building'
                counter['blocked'] += bool(row['source_error'])
        return dict(rows=rows, counters=counters, ready_batches=ready,
                    jobs=jobs, last_sync=snapshot['started_at'], snapshot_day=snapshot['day'], sync=dict(sync) if sync else None,
                    worker_healthy=bool(worker and 0 <= (now - worker['last_seen']).total_seconds() < 90))

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
