# поток: print
"""Integration tests use only a new disposable print_test_* PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
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

    def test_sync_progress_is_visible_while_running_and_completes_with_snapshot(self):
        from print_service.service import PrintService
        started, release = threading.Event(), threading.Event()
        rows = self.rows

        class NoNetwork:
            lease_check = staticmethod(lambda: None)

        class PausedCollector:
            def collect(self, *, now, on_progress):
                on_progress(stage='warehouses', processed=1, total=len(rows))
                started.set()
                if not release.wait(5): raise TimeoutError('test_release_timeout')
                return rows

        service = PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,
                               clock=lambda: NOW+timedelta(seconds=1))
        service.collector = PausedCollector()
        sid = self.queue.enqueue_sync();self.queue.start_sync()
        worker = threading.Thread(target=service.run_sync,args=(sid,))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            status = service.status()
            self.assertEqual(status['sync']['state'],'running')
            self.assertEqual(status['sync']['progress']['processed'],1)
            self.assertEqual(status['sync']['progress']['total'],len(rows))
            self.assertEqual(len(status['rows']),len(rows))
        finally:
            release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        status = service.status()
        self.assertEqual(status['sync']['state'],'ready')
        self.assertEqual(status['sync']['progress']['stage'],'complete')
        self.assertEqual(len(status['rows']),len(rows))

    def test_unwritable_progress_does_not_fail_sync(self):
        from print_service.service import PrintService
        from unittest.mock import patch
        rows = self.rows

        class NoNetwork:
            lease_check = staticmethod(lambda: None)

        class SavedCollector:
            def collect(self, *, now, on_progress):
                on_progress(stage='orders',loaded=len(rows))
                return rows

        service = PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,
                               clock=lambda: NOW+timedelta(seconds=1))
        service.collector = SavedCollector()
        sid=self.queue.enqueue_sync();self.queue.start_sync()
        with patch('print_service.service.save_json',side_effect=OSError('unavailable')):
            service.run_sync(sid)
        self.assertEqual(service.status()['sync']['state'],'ready')

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

    def test_explicit_ready_subset_leaves_other_orders_in_counter(self):
        self.rows.append(order('100-0003-1'))
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        chosen=[self.rows[0]['raw']['id'],self.rows[2]['raw']['id']]
        batch=self.queue.reserve_counter(self.counter,request_key='selected',now=NOW,order_ids=chosen)
        self.assertEqual({m['ms_order_id'] for m in batch['orders']},set(chosen))
        repeated=self.queue.reserve_counter(self.counter,request_key='selected',now=NOW,order_ids=list(reversed(chosen)))
        self.assertEqual(batch['id'],repeated['id'])
        with self.assertRaises(PrintBlocked):
            self.queue.reserve_counter(self.counter,request_key='selected',now=NOW,order_ids=[self.rows[1]['raw']['id']])
        pair=self.prepare(batch)
        self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        view=self.queue.view(now=NOW)
        self.assertEqual(sum(r['printed'] for r in view['rows']),2)
        self.assertEqual(view['counters'][self.counter]['count'],1)
        with self.assertRaises(PrintBlocked):
            self.queue.reserve_counter(self.counter,request_key='duplicate',now=NOW,order_ids=chosen)

    def test_ms_printed_orders_are_tagged_excluded_and_individually_printable(self):
        self.rows[0]['display']={'ms_demand_printed':True,'ms_order_printed':False}
        self.rows[1]['display']={'ms_demand_printed':False}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        view=self.queue.view(now=NOW)
        self.assertEqual(view['counters'][self.counter]['count'],1)
        self.assertEqual(view['counters'][self.counter]['ms_printed'],1)
        tagged=next(r for r in view['rows'] if str(r['id'])==self.rows[0]['raw']['id'])
        self.assertTrue(tagged['ms_printed']);self.assertFalse(tagged['printed'])
        bulk=self.reserve('bulk');self.assertEqual(len(bulk['orders']),1)
        self.assertEqual(bulk['orders'][0]['ms_order_id'],self.rows[1]['raw']['id'])
        pair=self.prepare(bulk);self.queue.publish_pair(bulk['id'],generation=1,pair_dir=pair)
        individual=self.queue.reserve_counter(self.counter,request_key='one',now=NOW,order_ids=[self.rows[0]['raw']['id']])
        self.assertEqual(len(individual['orders']),1)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)

    def test_prefetched_label_does_not_reserve_orders_export_receipts_or_change_counters(self):
        oid,did=self.rows[0]['raw']['id'],self.rows[0]['demand_id']
        label=self.root/'prefetched.pdf';pdf(label,164.25,113.25)
        self.assertEqual(len(self.queue.label_candidates(now=NOW)),2)
        self.assertTrue(self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW))
        view=self.queue.view(now=NOW);row=next(r for r in view['rows'] if str(r['id'])==oid)
        self.assertTrue(row['label_ready']);self.assertFalse(row['receipt_ready']);self.assertFalse(row['printed'])
        self.assertEqual(view['counters'][self.counter]['count'],2)
        self.assertEqual(view['ready_batches'],[])
        from print_service.labels import LabelPrefetcher
        candidates=LabelPrefetcher(self.queue,object(),root=self.root).candidates(NOW)
        self.assertEqual([str(r['id']) for r in candidates],[self.rows[1]['raw']['id']])

    def test_background_labels_cannot_overwrite_claimed_or_obsolete_orders(self):
        oid,did=self.rows[0]['raw']['id'],self.rows[0]['demand_id']
        label=self.root/'prefetched.pdf';pdf(label,164.25,113.25)
        self.assertFalse(self.queue.mark_prefetched_label(oid,demand_id=str(uuid4()),path=label,now=NOW))
        self.assertFalse(self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW+timedelta(days=1)))
        batch=self.reserve();pair=self.prepare(batch)
        self.assertEqual(self.queue.label_candidates(now=NOW),[])
        self.assertFalse(self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW))
        self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        self.assertFalse(self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW))
        self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']),1)

    def test_failed_jobs_hide_ms_printed_ready_and_out_of_scope_orders(self):
        failed=self.reserve('failed')
        self.queue.fail_batch(failed['id'],generation=1,error='order_changed_since_counter_click')
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual({o['external_id'] for o in jobs[0]['orders']},{r['raw']['name'] for r in self.rows})
        self.rows[0]['display']={'ms_demand_printed':True}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual([o['external_id'] for o in jobs[0]['orders']],[self.rows[1]['raw']['name']])
        done=self.reserve('done');pair=self.prepare(done)
        self.queue.publish_pair(done['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])
        self.queue.sync_snapshot([],started_at=NOW+timedelta(seconds=2),complete=True)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])

    def test_preparation_records_the_failing_order_and_hides_its_resolved_error(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        batch=self.reserve()
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with patch.object(Preparer,'_one',side_effect=[object(),PrintBlocked('order_changed_since_counter_click')]):
            service.run_batch(batch)
        failed_id=batch['orders'][1]['ms_order_id']
        failed=self.queue.batch(batch['id'])
        self.assertEqual(failed['display']['error_order_id'],failed_id)
        job=self.queue.view(now=NOW)['jobs'][0]
        self.assertEqual([o['ms_order_id'] for o in job['orders']],[failed_id])
        for row in self.rows:
            if row['raw']['id']==failed_id:row['display']={'ms_demand_printed':True}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],1)

    def test_repeated_failed_jobs_only_show_latest_error_per_order(self):
        old=self.reserve('old');self.queue.fail_batch(old['id'],generation=1,error='old')
        latest=self.reserve('latest');self.queue.fail_batch(latest['id'],generation=1,error='latest')
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual([str(j['id']) for j in jobs],[str(latest['id'])])
        self.assertEqual(jobs[0]['error'],'latest')

    def test_ms_flag_set_during_build_does_not_remove_unpublished_bulk_orders(self):
        batch=self.reserve()
        for row in self.rows:row['display']={'ms_demand_printed':True}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],2)
        pair=self.prepare(batch);self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)

    def test_single_order_pair_and_cached_download_never_repeat_provider_requests(self):
        from fastapi.testclient import TestClient
        from print_service.service import PrintService
        from web.print_app import create_app
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        self.queue.heartbeat(NOW)
        oid=self.rows[0]['raw']['id']
        with TestClient(create_app(lambda:service),headers={'X-Pult-User':'print'}) as client:
            response=client.post(f'/api/print/orders/{oid}/pair',json={'request_key':str(uuid4())})
            self.assertEqual(response.status_code,200)
            batch=self.queue.batch(response.json()['id'])
            self.assertEqual([m['ms_order_id'] for m in batch['orders']],[oid])
            pair=self.prepare(batch);self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
            for kind in ('receipts','labels'):
                url=f'/api/print/orders/{oid}/files/{kind}'
                first=client.get(url);again=client.get(url)
                self.assertEqual(first.status_code,200);self.assertEqual(first.content,again.content)
                from io import BytesIO
                from pypdf import PdfReader
                self.assertEqual(len(PdfReader(BytesIO(first.content)).pages),1)
            result=client.post(f'/api/print/orders/{oid}/pair',json={'request_key':str(uuid4())})
            self.assertEqual(result.json()['state'],'ready')
            self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']),1)
            self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],1)

    def test_individual_reprint_from_a_group_does_not_create_another_batch(self):
        from print_service.service import PrintService
        from io import BytesIO
        from pypdf import PdfReader
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        group=self.reserve();pair=self.prepare(group)
        self.queue.publish_pair(group['id'],generation=1,pair_dir=pair)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        oid=self.rows[1]['raw']['id']
        result=service.request_order(oid,str(uuid4()))
        self.assertEqual(result['state'],'ready')
        files=service.order_files(oid)
        self.assertEqual(len(PdfReader(files['receipts']).pages),1)
        self.assertEqual(len(PdfReader(files['labels']).pages),1)
        self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']),1)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)

    def test_unavailable_selected_order_does_not_reserve_the_remaining_order(self):
        with self.assertRaises(PrintBlocked):
            self.queue.reserve_counter(self.counter,request_key='missing',now=NOW,
                order_ids=[self.rows[0]['raw']['id'],str(uuid4())])
        with self.queue.transaction() as cursor:
            cursor.execute('SELECT count(*) AS count FROM claims')
            self.assertEqual(cursor.fetchone()['count'],0)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],2)

    def test_retry_failed_batch_keeps_frozen_members_and_new_arrivals_queued(self):
        batch=self.reserve()
        self.queue.fail_batch(batch['id'],generation=1,error='order_changed_since_counter_click')
        self.rows.append(order('100-0003-1'))
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        resumed=self.queue.retry_failed_batch(batch['id'],generation=1)
        self.assertEqual(resumed['orders'],batch['orders'])
        self.assertEqual(resumed['generation'],2)
        with self.assertRaises(PrintBlocked):self.queue.retry_failed_batch(batch['id'],generation=1)
        pair=self.prepare(resumed)
        self.queue.publish_pair(resumed['id'],generation=2,pair_dir=pair)
        view=self.queue.view(now=NOW)
        self.assertEqual(sum(r['printed'] for r in view['rows']),2)
        self.assertEqual(view['counters'][self.counter]['count'],1)
        self.assertEqual(len(view['ready_batches']),1)

    def test_retry_failed_batch_cannot_take_orders_claimed_by_another_pair(self):
        original=self.reserve()
        self.queue.fail_batch(original['id'],generation=1,error='sample')
        other=self.reserve('new-click')
        with self.assertRaises(PrintBlocked):self.queue.retry_failed_batch(original['id'],generation=1)
        pair=self.prepare(other)
        self.queue.publish_pair(other['id'],generation=1,pair_dir=pair)
        with self.assertRaises(PrintBlocked):self.queue.retry_failed_batch(original['id'],generation=1)
        self.assertEqual(self.queue.batch(original['id'])['state'],'failed')
        self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']),1)

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

    def test_diskver_login_cannot_download_another_organizations_stored_pair(self):
        from fastapi.testclient import TestClient
        from print_service.service import PrintService
        from web.print_app import create_app
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        self.rows[0]['counter_key']='oz_acc1|digital|store'
        diskver=order('200-0001-1',counter='oz_acc2|diskver|store')
        diskver['account']='oz_acc2'
        self.rows=[self.rows[0],diskver]
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        bids={}
        for row in self.rows:
            batch=self.queue.reserve_counter(row['counter_key'],request_key=str(uuid4()),now=NOW)
            pair=self.prepare(batch)
            self.queue.publish_pair(batch['id'],generation=batch['generation'],pair_dir=pair)
            bids[row['account']]=batch['id']
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with TestClient(create_app(lambda:service),headers={'X-Pult-User':'tolkachev'}) as client:
            status=client.get('/api/print').json()
            self.assertEqual([r['account'] for r in status['rows']],['oz_acc2'])
            self.assertEqual([b['id'] for b in status['ready_batches']],[str(bids['oz_acc2'])])
            for kind in ('receipts','labels'):
                digital=f"/api/print/batches/{bids['oz_acc1']}/files/{kind}"
                diskver=f"/api/print/batches/{bids['oz_acc2']}/files/{kind}"
                self.assertEqual(client.get(digital).status_code,404)
                self.assertEqual(client.get(diskver).status_code,200)
                self.assertEqual(client.get(digital,headers={'X-Pult-User':'print'}).status_code,200)

    def test_changed_demand_cannot_publish_a_stale_pair(self):
        batch=self.reserve();pair=self.prepare(batch)
        self.rows[0]['demand_id']=str(uuid4())
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(minutes=1),complete=True)
        with self.assertRaises(PrintBlocked):self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['ready_batches'],[])
