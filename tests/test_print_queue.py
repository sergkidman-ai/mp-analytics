# поток: print
"""Integration tests use only a new disposable print_test_* PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg2 import sql
from pypdf import PdfWriter

from core.db import get_conn
from print_service.domain import OrderPrintFiles, PrintBlocked
from print_service.pdf import build_pair
from print_service.queue import PrintQueue


NOW = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)


def order(number, *, counter='oz_acc1:direction_1:ms_store_1', day='2026-11-03'):
    oid, did = str(uuid4()), str(uuid4())
    return dict(account='oz_acc1', demand_id=did, counter_key=counter,
                raw={'id': oid, 'name': number, 'state': {'name': 'Собирается'},
                     'deliveryPlannedMoment': day + ' 12:00:00'})


def pdf(path, width=612, height=792):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    writer.write(str(path))


class PrintQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'print_test_' + uuid4().hex
        cls.queue = PrintQueue(schema=cls.schema)
        cls.queue.install()

    @classmethod
    def tearDownClass(cls):
        assert cls.schema.startswith('print_test_')
        with get_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with self.queue.transaction() as cursor:
            cursor.execute('TRUNCATE artifacts,requests,claims,members,batches,orders,current_snapshot,snapshots,sync_runs,worker_status')
            cursor.execute('INSERT INTO current_snapshot(singleton) VALUES(true)')
        self.rows = [order('100-0001-1'), order('100-0002-1')]
        self.counter = self.rows[0]['counter_key']
        self.queue.sync_snapshot(self.rows, started_at=NOW, complete=True)

    def prepare(self, batch):
        files = []
        for member in batch['orders']:
            oid, did = member['ms_order_id'], member['ms_demand_id']
            receipt = self.root / (oid + '-receipt.pdf')
            label = self.root / (oid + '-label.pdf')
            if not receipt.exists(): pdf(receipt)
            if not label.exists(): pdf(label, 164.25, 113.25)
            self.queue.mark_artifact(oid, demand_id=did, kind='receipt', path=receipt)
            self.queue.mark_artifact(oid, demand_id=did, kind='label', path=label)
            files.append(OrderPrintFiles(member['account'], member['external_id'], oid, did,
                                        (str(receipt),), (str(label),)))
        return build_pair(tuple(files), shipment_date=batch['day'], output_root=self.root / 'pairs')

    def reserve(self, request='click'):
        return self.queue.reserve_counter(self.counter, request_key=request, now=NOW)

    def test_two_files_turn_rows_green_new_arrivals_accumulate_and_reopening_is_read_only(self):
        before = self.queue.view(now=NOW)
        self.assertEqual(before['counters'][self.counter]['count'], 2)
        self.assertTrue(all(r['color'] == 'red' and not r['receipt_ready'] and not r['label_ready'] for r in before['rows']))
        batch = self.reserve()
        pair = self.prepare(batch)
        self.assertTrue(all(r['color'] == 'red' and r['receipt_ready'] and r['label_ready'] for r in self.queue.view(now=NOW)['rows']))
        self.queue.publish_pair(batch['id'], generation=1, pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'], 0)
        self.rows.append(order('100-0003-1'))
        self.queue.sync_snapshot(self.rows, started_at=NOW + timedelta(minutes=30), complete=True)
        view = self.queue.view(now=NOW)
        self.assertEqual([r['color'] for r in view['rows']], ['green', 'green', 'red'])
        self.assertEqual(view['counters'][self.counter]['count'], 1)
        self.assertEqual(len(view['ready_batches']), 1)
        first = self.queue.ready_files(batch['id'])
        self.assertEqual(first, self.queue.ready_files(batch['id']))
        self.assertEqual(first['labels'].read_bytes(), (pair / 'labels.pdf').read_bytes())
        restarted = PrintQueue(schema=self.schema)
        self.assertEqual(len(restarted.view(now=NOW)['ready_batches']), 1)

    def test_concurrent_clicks_reserve_one_fixed_sequence(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: self.reserve(key), ('click-a','click-b')))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(len(results[0]['orders']), 2)
        self.rows.append(order('new-arrival'))
        self.queue.sync_snapshot(self.rows, started_at=NOW + timedelta(minutes=1), complete=True)
        pair = self.prepare(results[0])
        self.queue.publish_pair(results[0]['id'], generation=1, pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'], 1)
        self.assertEqual(self.reserve('click-b')['id'], results[0]['id'])
        self.assertEqual(len(self.reserve('next-click')['orders']), 1)

    def test_partial_failure_keeps_red_rows_and_received_artifacts_for_retry(self):
        batch = self.reserve()
        member = batch['orders'][0]
        receipt = self.root / (member['ms_order_id'] + '-receipt.pdf')
        pdf(receipt)
        self.queue.mark_artifact(member['ms_order_id'], demand_id=member['ms_demand_id'], kind='receipt', path=receipt)
        self.queue.fail_batch(batch['id'], generation=1, error='label_unavailable')
        view = self.queue.view(now=NOW)
        self.assertEqual(view['counters'][self.counter]['count'], 2)
        self.assertTrue(view['rows'][0]['receipt_ready'])
        self.assertFalse(view['rows'][0]['label_ready'])
        self.assertEqual(view['ready_batches'], [])
        retry = self.reserve('retry-click')
        pair = self.prepare(retry)
        with self.assertRaises(PrintBlocked):
            self.queue.publish_pair(batch['id'], generation=1, pair_dir=pair)
        self.queue.publish_pair(retry['id'], generation=1, pair_dir=pair)
        self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']), 1)

    def test_missing_second_pdf_never_marks_any_order_printed(self):
        batch = self.reserve()
        pair = self.prepare(batch)
        (pair / 'labels.pdf').unlink()
        with self.assertRaises(PrintBlocked):
            self.queue.publish_pair(batch['id'], generation=1, pair_dir=pair)
        with self.assertRaises(PrintBlocked):
            self.queue.ready_files(batch['id'])
        view = self.queue.view(now=NOW)
        self.assertTrue(all(r['color'] == 'red' for r in view['rows']))
        self.assertEqual(view['ready_batches'], [])

    def test_stale_worker_cannot_publish_or_release_recovered_work(self):
        batch = self.reserve()
        generation = self.queue.recover_batch(batch['id'], generation=1)
        pair = self.prepare(batch)
        with self.assertRaises(PrintBlocked):
            self.queue.publish_pair(batch['id'], generation=1, pair_dir=pair)
        with self.assertRaises(PrintBlocked):
            self.queue.fail_batch(batch['id'], generation=1, error='stale')
        self.queue.publish_pair(batch['id'], generation=generation, pair_dir=pair)
        with self.assertRaises(PrintBlocked):
            self.queue.fail_batch(batch['id'], generation=generation, error='too_late')

    def test_partial_or_older_sync_and_midnight_do_not_erase_print_history(self):
        with self.assertRaises(PrintBlocked):
            self.queue.sync_snapshot([], started_at=NOW + timedelta(minutes=1), complete=False)
        self.queue.sync_snapshot([], started_at=NOW - timedelta(minutes=1), complete=True)
        self.assertEqual(len(self.queue.view(now=NOW)['rows']), 2)
        batch = self.reserve()
        self.queue.publish_pair(batch['id'], generation=1, pair_dir=self.prepare(batch))
        tomorrow = NOW + timedelta(days=1)
        self.queue.sync_snapshot([], started_at=tomorrow, complete=True)
        view = self.queue.view(now=tomorrow)
        self.assertEqual(view['rows'], [])
        self.assertEqual(len(view['ready_batches']), 1)
        self.assertTrue(self.queue.ready_files(batch['id'])['receipts'].is_file())
        for row in self.rows: row['raw']['deliveryPlannedMoment'] = '2026-11-04 12:00:00'
        self.queue.sync_snapshot(self.rows, started_at=tomorrow + timedelta(minutes=1), complete=True)
        self.assertEqual(self.queue.view(now=tomorrow)['counters'][self.counter]['count'], 0)

    def test_artifact_version_change_invalidates_old_partner_file(self):
        row=self.rows[0];oid=row['raw']['id'];did=row['demand_id'];path=self.root/'artifact.pdf';pdf(path)
        self.queue.mark_artifact(oid,demand_id=did,kind='receipt',path=path,source_key='old')
        self.queue.mark_artifact(oid,demand_id=did,kind='label',path=path,source_key='new')
        current=self.queue.view(now=NOW)['rows'][0]
        self.assertFalse(current['receipt_ready']);self.assertTrue(current['label_ready'])

    def test_http_page_counter_publication_and_repeat_open_never_call_providers(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from print_service.service import PrintService
        from web.print_api import make_router
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        app=FastAPI();app.include_router(make_router(lambda:service))
        with TestClient(app) as client:
            self.assertEqual(client.get('/warehouse/print').status_code,200)
            request=dict(counter_key=self.counter,request_key=str(uuid4()))
            self.assertEqual(client.post('/api/print/counter',json=request).status_code,409)
            self.queue.heartbeat(NOW)
            result=client.post('/api/print/counter',json=request);self.assertEqual(result.status_code,200)
            batch=self.queue.batch(result.json()['id']);pair=self.prepare(batch)
            self.queue.publish_pair(batch['id'],generation=batch['generation'],pair_dir=pair)
            data=client.get('/api/print').json()
            self.assertTrue(all(r['printed'] for r in data['rows']))
            self.assertNotIn('receipt_path',data['rows'][0])
            for kind in ('receipts','labels'):
                url=f"/api/print/batches/{batch['id']}/files/{kind}"
                first=client.get(url);second=client.get(url)
                self.assertEqual(first.status_code,200);self.assertEqual(first.content,second.content)
                self.assertIn('inline',first.headers['content-disposition'])
            self.assertEqual(client.get(f"/api/print/batches/{batch['id']}/files/other").status_code,422)
            sid=client.post('/api/print/sync',json={}).json()['id']
            self.assertEqual(client.post('/api/print/sync',json={}).json()['id'],sid)

    def test_changed_demand_cannot_publish_a_stale_pair(self):
        batch=self.reserve();pair=self.prepare(batch)
        self.rows[0]['demand_id']=str(uuid4())
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(minutes=1),complete=True)
        with self.assertRaises(PrintBlocked):self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['ready_batches'],[])
