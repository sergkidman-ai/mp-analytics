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
            cursor.execute('TRUNCATE label_requests,external_prints,artifacts,requests,claims,members,batches,orders,current_snapshot,snapshots,sync_runs,worker_status')
            cursor.execute('INSERT INTO current_snapshot(singleton) VALUES(true)')
        self.rows = [order('100-0001-1'), order('100-0002-1')]
        self.counter = self.rows[0]['counter_key']
        self.queue.sync_snapshot(self.rows, started_at=NOW, complete=True)

    def prepare(self, batch):
        files = [self.prepared_order(member) for member in batch['orders']]
        return build_pair(tuple(files), shipment_date=batch['day'], output_root=self.root / 'pairs')

    def prepared_order(self, member):
        oid,did=member['ms_order_id'],member['ms_demand_id']
        receipt=self.root/(oid+'-receipt.pdf');label=self.root/(oid+'-label.pdf')
        if not receipt.exists():pdf(receipt)
        if not label.exists():pdf(label,164.25,113.25)
        self.queue.mark_artifact(oid,demand_id=did,kind='receipt',path=receipt)
        self.queue.mark_artifact(oid,demand_id=did,kind='label',path=label)
        return OrderPrintFiles(member['account'],member['external_id'],oid,did,(str(receipt),),(str(label),))

    def reserve(self, request='click'):
        return self.queue.reserve_counter(self.counter, request_key=request, now=NOW)

    def test_old_demand_failure_does_not_block_replacement_shipment(self):
        failed=self.reserve('old-demand')
        self.queue.fail_batch(failed['id'],generation=1,error='order_demand_ambiguous')
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)
        self.rows[0]['demand_id']=str(uuid4())
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        status=self.queue.view(now=NOW+timedelta(seconds=2))
        changed=next(r for r in status['rows'] if str(r['id'])==self.rows[0]['raw']['id'])
        self.assertIsNone(changed['preparation_error']);self.assertTrue(changed['counter_eligible'])
        self.assertEqual(status['counters'][self.counter]['count'],1)
        self.assertEqual(status['jobs'][0]['orders'][0]['ms_order_id'],self.rows[1]['raw']['id'])
        retry=self.queue.reserve_counter(self.counter,request_key='replacement',now=NOW+timedelta(seconds=2))
        self.assertEqual([m['ms_demand_id'] for m in retry['orders']],[self.rows[0]['demand_id']])

    def test_manual_label_refresh_is_async_idempotent_and_never_prints_receipts(self):
        from tests.test_print_labels import SavedLabels
        from print_service.service import PrintService
        clients=SavedLabels('ozon');clients.order['deliveryPlannedMoment']='2026-11-03 12:00:00'
        row=dict(raw=clients.order,account=clients.row['account'],demand_id=clients.row['demand_id'],counter_key='oz_acc1|test|store')
        clock=NOW+timedelta(seconds=1)
        self.queue.sync_snapshot([row],started_at=clock,complete=True);self.queue.heartbeat(clock)
        service=PrintService(queue=self.queue,clients=clients,root=self.root,clock=lambda:clock)
        first=service.request_label(clients.order['id'],'refresh-one')
        same=service.request_label(clients.order['id'],'refresh-two')
        self.assertEqual(first['id'],same['id']);self.assertEqual(first['state'],'pending')
        self.assertEqual(clients.downloads,0);self.assertEqual(clients.exports,0)
        task=self.queue.start_label_request(now=clock);service.run_label_request(task)
        status=service.status();result=status['rows'][0]
        self.assertEqual(result['label_refresh']['state'],'ready')
        self.assertTrue(result['label_ready']);self.assertFalse(result['receipt_ready']);self.assertFalse(result['printed'])
        self.assertEqual(status['counters'][row['counter_key']]['count'],1)
        self.assertEqual(status['ready_batches'],[]);self.assertEqual(clients.exports,0);self.assertEqual(clients.downloads,1)
        self.assertEqual(self.queue.enqueue_label_request(clients.order['id'],request_key='refresh-one',now=clock)['id'],first['id'])
        # A second explicit refresh verifies the already cached native label.
        service.request_label(clients.order['id'],'refresh-three');service.run_label_request(self.queue.start_label_request(now=clock))
        self.assertEqual(clients.downloads,1);self.assertEqual(clients.exports,0)
        pair=self.queue.reserve_counter(row['counter_key'],request_key='warehouse-click',now=clock)
        with self.assertRaises(PrintBlocked):service.request_label(clients.order['id'],'during-print')
        self.assertEqual(len(pair['orders']),1)

    def test_failed_label_refresh_is_visible_and_excluded_until_fresh_success(self):
        from unittest.mock import patch
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        self.queue.heartbeat(NOW);service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        oid=self.rows[0]['raw']['id'];service.request_label(oid,'label-error')
        with patch.object(service.labels,'one',side_effect=PrintBlocked('ozon_posting_not_ready_for_label')):
            service.run_label_request(self.queue.start_label_request(now=NOW))
        status=service.status();row=next(r for r in status['rows'] if str(r['id'])==oid)
        self.assertEqual(row['label_error'],'ozon_posting_not_ready_for_label')
        self.assertEqual(status['counters'][self.counter]['count'],1)
        batch=self.reserve('healthy-other');self.assertEqual([m['ms_order_id'] for m in batch['orders']],[self.rows[1]['raw']['id']])
        label=self.root/'fresh-label.pdf';pdf(label,164.25,113.25)
        self.assertTrue(self.queue.mark_prefetched_label(oid,demand_id=self.rows[0]['demand_id'],path=label,now=NOW))
        self.queue.resolve_label_errors(oid,demand_id=self.rows[0]['demand_id'],now=NOW)
        row=next(r for r in service.status()['rows'] if str(r['id'])==oid)
        self.assertIsNone(row['label_error']);self.assertTrue(row['counter_eligible'])

    def test_express_labels_have_priority_over_ordinary_orders(self):
        self.rows[1]['counter_key']='oz_acc1|z-express|store'
        self.rows[1]['display']={'color':{'key':'express','short':'ЭКС'}}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        candidates=self.queue.label_candidates(now=NOW)
        self.assertEqual([str(r['id']) for r in candidates],
                         [self.rows[1]['raw']['id'],self.rows[0]['raw']['id']])

    def test_counter_excludes_known_errors_and_reserves_only_healthy_orders(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        self.rows.extend([order('100-0003-1'),order('100-0004-1')])
        missing,failed=self.rows[2],self.rows[3]
        missing.update(demand_id=None,source_error='linked_demand_missing')
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        old=self.queue.reserve_counter(self.counter,request_key='old-failure',now=NOW,order_ids=[failed['raw']['id']])
        self.queue.fail_batch(old['id'],generation=1,error='wb_order_not_ready_for_label')
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        status=service.status();counter=status['counters'][self.counter]
        self.assertEqual(counter,dict(count=2,building=False,blocked=2))
        self.assertEqual(sum(r['counter_eligible'] for r in status['rows']),2)
        batch=self.reserve('healthy-click')
        good={r['raw']['id'] for r in self.rows[:2]}
        self.assertEqual({m['ms_order_id'] for m in batch['orders']},good)
        self.assertEqual(batch['display']['blocked_orders'],{})
        with patch.object(Preparer,'_one',side_effect=lambda m,day:self.prepared_order(m)) as prepare:
            service.run_batch(batch)
        self.assertEqual(prepare.call_count,2)
        ready=self.queue.batch(batch['id']);self.assertEqual(ready['state'],'ready')
        self.assertEqual({m['ms_order_id'] for m in ready['manifest']['orders']},good)
        status=service.status();self.assertEqual(status['counters'][self.counter]['count'],0)
        self.assertEqual(status['counters'][self.counter]['blocked'],2)
        with self.assertRaises(PrintBlocked):self.reserve('all-errors')
        # Corrected source data re-enters the counter without generating documents.
        missing.update(demand_id=str(uuid4()),source_error=None)
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=2),complete=True)
        self.assertEqual(service.status()['counters'][self.counter]['count'],1)
        # A deliberate per-order retry is still available for an excluded failure.
        retry=self.queue.reserve_counter(self.counter,request_key='individual-retry',now=NOW,order_ids=[failed['raw']['id']])
        self.assertEqual([m['ms_order_id'] for m in retry['orders']],[failed['raw']['id']])

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
        self.assertEqual(view['counters'][self.counter]['count'], 0)
        self.assertEqual(view['counters'][self.counter]['blocked'],2)
        self.assertTrue(view['rows'][0]['receipt_ready'])
        self.assertFalse(view['rows'][0]['label_ready'])
        self.assertEqual(view['ready_batches'], [])
        retry = self.queue.reserve_counter(self.counter,request_key='retry-click',now=NOW,order_ids=[r['raw']['id'] for r in self.rows])
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

    def test_ms_print_flags_do_not_exclude_orders_from_counters_or_reservation(self):
        self.rows[0]['display']={'ms_demand_printed':True,'ms_order_printed':False}
        self.rows[1]['display']={'ms_demand_printed':False}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        view=self.queue.view(now=NOW)
        self.assertEqual(view['counters'][self.counter]['count'],2)
        self.assertNotIn('ms_printed',view['counters'][self.counter])
        tagged=next(r for r in view['rows'] if str(r['id'])==self.rows[0]['raw']['id'])
        self.assertNotIn('ms_printed',tagged);self.assertFalse(tagged['printed'])
        bulk=self.reserve('bulk');self.assertEqual(len(bulk['orders']),2)
        self.assertEqual({o['ms_order_id'] for o in bulk['orders']},{r['raw']['id'] for r in self.rows})
        pair=self.prepare(bulk);self.queue.publish_pair(bulk['id'],generation=1,pair_dir=pair)
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

    def test_successful_label_check_resolves_only_past_matching_label_errors(self):
        from print_service.labels import LabelPrefetcher
        batch=self.reserve();oid,did=self.rows[0]['raw']['id'],self.rows[0]['demand_id']
        self.queue.fail_batch(batch['id'],generation=1,error='wb_order_not_ready_for_label')
        with self.queue.transaction() as c:c.execute('UPDATE batches SET created_at=%s WHERE id=%s',(NOW,batch['id']))
        label=self.root/'verified.pdf';pdf(label,164.25,113.25)
        self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW)
        self.assertIn(oid,[str(r['id']) for r in LabelPrefetcher(self.queue,object(),root=self.root).candidates(NOW)])
        self.assertEqual(self.queue.resolve_label_errors(oid,demand_id=str(uuid4()),now=NOW+timedelta(seconds=1)),0)
        self.assertEqual(self.queue.resolve_label_errors(oid,demand_id=did,now=NOW+timedelta(seconds=1)),1)
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual([o['ms_order_id'] for o in jobs[0]['orders']],[self.rows[1]['raw']['id']])
        self.assertNotIn(oid,[str(r['id']) for r in LabelPrefetcher(self.queue,object(),root=self.root).candidates(NOW)])
        retry=self.queue.retry_failed_batch(batch['id'],generation=1)
        self.queue.fail_batch(retry['id'],generation=retry['generation'],error='wb_order_not_ready_for_label',order_id=oid)
        self.assertEqual(self.queue.view(now=NOW)['jobs'][0]['orders'][0]['ms_order_id'],oid)

    def test_label_confirmation_does_not_hide_receipt_failures_or_newer_label_failures(self):
        batch=self.reserve();oid,did=self.rows[0]['raw']['id'],self.rows[0]['demand_id']
        self.queue.fail_batch(batch['id'],generation=1,error='receipt_quantity_cell_too_small',order_id=oid)
        label=self.root/'verified.pdf';pdf(label,164.25,113.25)
        self.queue.mark_prefetched_label(oid,demand_id=did,path=label,now=NOW)
        self.assertEqual(self.queue.resolve_label_errors(oid,demand_id=did,now=NOW+timedelta(seconds=1)),0)
        self.assertEqual(len(self.queue.view(now=NOW)['jobs']),1)
        retry=self.queue.retry_failed_batch(batch['id'],generation=1)
        self.queue.fail_batch(retry['id'],generation=retry['generation'],error='wb_order_not_ready_for_label',order_id=oid)
        with self.queue.transaction() as c:c.execute('UPDATE batches SET created_at=%s WHERE id=%s',(NOW+timedelta(seconds=5),batch['id']))
        self.assertEqual(self.queue.resolve_label_errors(oid,demand_id=did,now=NOW),0)
        self.assertEqual(len(self.queue.view(now=NOW)['jobs']),1)

    def test_recovered_order_is_not_counted_as_excluded_in_its_published_pair(self):
        oid=self.rows[0]['raw']['id']
        batch=self.queue.reserve_counter(self.counter,request_key='single',now=NOW,order_ids=[oid])
        self.queue.exclude_failed_orders(batch['id'],generation=1,
            failures=[dict(ms_order_id=oid,error='receipt_quantity_cell_too_small')])
        retry=self.queue.retry_failed_batch(batch['id'],generation=1)
        pair=self.prepare(retry);self.queue.publish_pair(retry['id'],generation=retry['generation'],pair_dir=pair)
        display=self.queue.batch(batch['id'])['display']
        self.assertEqual(display['excluded_orders'],[])
        self.assertEqual(display['past_exclusions'][0]['ms_order_id'],oid)

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

    def test_failed_jobs_ignore_ms_print_flags_and_hide_ready_or_out_of_scope_orders(self):
        failed=self.reserve('failed')
        self.queue.fail_batch(failed['id'],generation=1,error='order_changed_since_counter_click')
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual({o['external_id'] for o in jobs[0]['orders']},{r['raw']['name'] for r in self.rows})
        self.rows[0]['display']={'ms_demand_printed':True}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual(len(jobs[0]['orders']),2)
        self.queue.sync_snapshot(self.rows[1:],started_at=NOW+timedelta(seconds=2),complete=True)
        jobs=self.queue.view(now=NOW)['jobs']
        self.assertEqual([o['external_id'] for o in jobs[0]['orders']],[self.rows[1]['raw']['name']])
        done=self.queue.reserve_counter(self.counter,request_key='done',now=NOW,order_ids=[self.rows[1]['raw']['id']]);pair=self.prepare(done)
        self.queue.publish_pair(done['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])
        self.queue.sync_snapshot([],started_at=NOW+timedelta(seconds=3),complete=True)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])

    def test_preparation_records_the_failing_order_and_hides_its_resolved_error(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        batch=self.reserve()
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        failed_id=batch['orders'][1]['ms_order_id']
        def prepare(member,day):
            if member['ms_order_id']==failed_id:raise PrintBlocked('order_changed_since_counter_click')
            return self.prepared_order(member)
        with patch.object(Preparer,'_one',side_effect=prepare):
            service.run_batch(batch)
        parent=self.queue.batch(batch['id'])
        self.assertEqual(parent['state'],'ready')
        self.assertEqual(parent['display']['excluded_orders'][0]['ms_order_id'],failed_id)
        job=self.queue.view(now=NOW)['jobs'][0]
        self.assertEqual([o['ms_order_id'] for o in job['orders']],[failed_id])
        for row in self.rows:
            if row['raw']['id']==failed_id:row['display']={'ms_demand_printed':True}
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        self.assertEqual(len(self.queue.view(now=NOW)['jobs']),1)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)
        done=self.queue.reserve_counter(self.counter,request_key='resolved',now=NOW,order_ids=[failed_id])
        pair=self.prepare(done);self.queue.publish_pair(done['id'],generation=1,pair_dir=pair)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)

    def test_partial_batch_keeps_order_sequence_counter_errors_and_request_idempotency(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        self.rows.extend([order('100-0003-1'),order('100-0004-1')])
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        selected=[r['raw']['id'] for r in self.rows]
        batch=self.queue.reserve_counter(self.counter,request_key='partial',now=NOW,order_ids=selected)
        bad={batch['orders'][0]['ms_order_id'],batch['orders'][-1]['ms_order_id']}
        def prepare(member,day):
            if member['ms_order_id'] in bad:raise PrintBlocked('receipt_quantity_cell_too_small')
            return self.prepared_order(member)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with patch.object(Preparer,'_one',side_effect=prepare):service.run_batch(batch)
        ready=self.queue.batch(batch['id']);self.assertEqual(ready['state'],'ready')
        expected=[m['ms_order_id'] for m in batch['orders'] if m['ms_order_id'] not in bad]
        self.assertEqual([m['ms_order_id'] for m in ready['orders']],expected)
        manifest=ready['manifest'];self.assertEqual([m['ms_order_id'] for m in manifest['orders']],expected)
        self.assertEqual(len(ready['display']['excluded_orders']),2)
        view=service.status();self.assertEqual(view['counters'][self.counter]['count'],0)
        self.assertEqual({o['ms_order_id'] for j in view['jobs'] for o in j['orders']},bad)
        self.assertTrue(all(r['preparation_error']=='receipt_quantity_cell_too_small' for r in view['rows'] if str(r['id']) in bad))
        repeated=self.queue.reserve_counter(self.counter,request_key='partial',now=NOW,order_ids=selected)
        self.assertEqual(str(repeated['id']),str(batch['id']))
        self.assertEqual(len(self.queue.view(now=NOW)['ready_batches']),1)

    def test_all_failed_orders_release_claims_and_preserve_individual_errors(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        batch=self.reserve();service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with patch.object(Preparer,'_one',side_effect=PrintBlocked('receipt_quantity_cell_too_small')):
            service.run_batch(batch)
        self.assertEqual(self.queue.batch(batch['id'])['state'],'failed')
        view=self.queue.view(now=NOW);self.assertEqual(view['ready_batches'],[])
        self.assertEqual(view['counters'][self.counter]['count'],0)
        self.assertEqual(len(view['jobs']),2)
        with self.queue.transaction() as c:
            c.execute('SELECT count(*) AS n FROM claims');self.assertEqual(c.fetchone()['n'],0)

    def test_known_blocked_order_does_not_prevent_other_orders_from_printing(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        bad=self.rows[0]['raw']['id'];self.rows[0].update(demand_id=None,source_error='linked_demand_missing')
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        batch=self.reserve();self.assertEqual([m['ms_order_id'] for m in batch['orders']],[self.rows[1]['raw']['id']]);service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        def prepare(member,day):
            self.assertNotEqual(member['ms_order_id'],bad)
            return self.prepared_order(member)
        with patch.object(Preparer,'_one',side_effect=prepare):service.run_batch(batch)
        self.assertEqual(self.queue.batch(batch['id'])['state'],'ready')
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['blocked'],1)
        self.assertEqual(self.queue.view(now=NOW)['jobs'],[])

    def test_every_excluded_order_keeps_its_error_in_a_large_batch(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        self.rows=[order(f'200-{i:04d}-1') for i in range(35)]
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        batch=self.reserve();service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with patch.object(Preparer,'_one',side_effect=PrintBlocked('receipt_quantity_cell_too_small')):
            service.run_batch(batch)
        status=service.status()
        self.assertEqual(len(status['jobs']),35)
        self.assertEqual(sum(bool(r['preparation_error']) for r in status['rows']),35)
        self.assertEqual(status['counters'][self.counter]['count'],0)

    def test_explicit_file_selection_combines_counters_preserves_file_order_and_is_idempotent(self):
        self.rows[1]['counter_key']='oz_acc1|other|store'
        self.rows.append(order('100-0003-1',counter='oz_acc1|third|store'))
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(seconds=1),complete=True)
        selected=[self.rows[i]['raw']['id'] for i in (2,0,1)]
        batch=self.queue.reserve_orders(selected,account='oz_acc1',request_key='file',now=NOW,title='Ozon из файла')
        self.assertEqual([m['ms_order_id'] for m in batch['orders']],selected)
        pair=self.prepare(batch);self.queue.publish_pair(batch['id'],generation=1,pair_dir=pair)
        view=self.queue.view(now=NOW)
        self.assertEqual(sum(c['count'] for c in view['counters'].values()),0)
        self.assertEqual(len(view['ready_batches']),1)
        repeated=self.queue.reserve_orders(selected,account='oz_acc1',request_key='file',now=NOW,title='Ozon из файла')
        self.assertEqual(str(repeated['id']),str(batch['id']))
        with self.assertRaises(PrintBlocked):
            self.queue.reserve_orders(selected,account='oz_acc1',request_key='again',now=NOW,title='Ozon из файла')

    def test_file_selection_rejects_foreign_missing_or_claimed_members_atomically(self):
        ids=[r['raw']['id'] for r in self.rows]
        for account,selected in (('oz_acc2',ids),('oz_acc1',ids+[str(uuid4())])):
            with self.assertRaises(PrintBlocked):
                self.queue.reserve_orders(selected,account=account,request_key=str(uuid4()),now=NOW,title='Выборка')
        batch=self.queue.reserve_counter(self.counter,request_key='one',now=NOW,order_ids=ids[:1])
        with self.assertRaises(PrintBlocked):
            self.queue.reserve_orders(ids,account='oz_acc1',request_key='file',now=NOW,title='Выборка')
        self.assertEqual(len(self.queue.batch(batch['id'])['orders']),1)
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],2)

    def test_partial_selection_survives_worker_recovery_and_rejects_stale_generation(self):
        from unittest.mock import patch
        from print_service.preparer import Preparer
        from print_service.service import PrintService
        class NoNetwork:
            lease_check=staticmethod(lambda:None)
        batch=self.reserve();failed=batch['orders'][1]['ms_order_id']
        self.prepared_order(batch['orders'][0])
        self.queue.exclude_failed_orders(batch['id'],generation=1,
            failures=[dict(ms_order_id=failed,error='receipt_quantity_cell_too_small')])
        generation=self.queue.recover_batch(batch['id'],generation=1)
        with self.assertRaises(PrintBlocked):
            self.queue.exclude_failed_orders(batch['id'],generation=1,
                failures=[dict(ms_order_id=batch['orders'][0]['ms_order_id'],error='stale')])
        resumed=self.queue.batch(batch['id']);self.assertEqual(resumed['generation'],generation)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with patch.object(Preparer,'_one',side_effect=lambda m,day:self.prepared_order(m)):service.run_batch(resumed)
        self.assertEqual(self.queue.batch(batch['id'])['state'],'ready')
        self.assertEqual(self.queue.view(now=NOW)['counters'][self.counter]['count'],0)

    def test_repeated_failed_jobs_only_show_latest_error_per_order(self):
        old=self.reserve('old');self.queue.fail_batch(old['id'],generation=1,error='old')
        latest=self.queue.reserve_counter(self.counter,request_key='latest',now=NOW,order_ids=[r['raw']['id'] for r in self.rows]);self.queue.fail_batch(latest['id'],generation=1,error='latest')
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
        other=self.queue.reserve_counter(self.counter,request_key='new-click',now=NOW,order_ids=[r['raw']['id'] for r in self.rows])
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
        self.assertEqual(view['ready_batches'], [])
        self.assertEqual(len(self.queue.archive(now=tomorrow)['ready_batches']), 1)
        self.assertTrue(self.queue.ready_files(batch['id'])['receipts'].is_file())
        for row in self.rows: row['raw']['deliveryPlannedMoment'] = '2026-11-04 12:00:00'
        self.queue.sync_snapshot(self.rows, started_at=tomorrow + timedelta(minutes=1), complete=True)
        self.assertEqual(self.queue.view(now=tomorrow)['counters'][self.counter]['count'], 0)

    def test_archive_preserves_removed_and_rescheduled_orders_and_native_files(self):
        from copy import deepcopy
        from hashlib import sha256
        self.rows[0]['raw']['store'] = {'name': 'Вчерашний склад'}
        self.queue.sync_snapshot(self.rows, started_at=NOW+timedelta(seconds=1), complete=True)
        batch = self.queue.reserve_counter(self.counter, request_key='one', now=NOW,
                                           order_ids=[self.rows[0]['raw']['id']])
        self.queue.publish_pair(batch['id'], generation=1, pair_dir=self.prepare(batch))
        paths = self.queue.ready_files(batch['id'])
        hashes = {key: sha256(path.read_bytes()).hexdigest() for key,path in paths.items()}
        # The second order disappears even from the last snapshot of the day.
        self.queue.sync_snapshot(self.rows[:1], started_at=NOW+timedelta(minutes=1), complete=True)
        tomorrow = NOW+timedelta(days=1)
        moved = deepcopy(self.rows[:1])
        moved[0]['raw']['deliveryPlannedMoment'] = '2026-11-04 12:00:00'
        moved[0]['raw']['store']['name'] = 'Новый склад'
        moved[0]['display'] = dict(direction='Новое направление', store='Новый склад')
        self.queue.sync_snapshot(moved, started_at=tomorrow, complete=True)
        archive = self.queue.archive(now=tomorrow)
        self.assertEqual(archive['day'], NOW.date())
        self.assertEqual([(d['order_count'],d['printed_count']) for d in archive['days']], [(2,1)])
        rows = {r['external_id']:r for r in archive['rows']}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows['100-0001-1']['printed'])
        self.assertEqual(rows['100-0001-1']['display']['store'], 'Вчерашний склад')
        self.assertEqual(rows['100-0001-1']['display']['ms_status'], 'Собирается')
        self.assertFalse(rows['100-0002-1']['printed'])
        self.assertEqual(self.queue.view(now=tomorrow)['ready_batches'], [])
        self.assertEqual(hashes, {key:sha256(path.read_bytes()).hexdigest() for key,path in paths.items()})

    def test_archive_dates_scope_month_boundary_and_moscow_midnight(self):
        from copy import deepcopy
        diskver = order('DISKVER', counter='oz_acc2|direction|store', day='2026-11-04')
        diskver['account'] = 'oz_acc2'
        self.queue.sync_snapshot([diskver], started_at=NOW+timedelta(days=1), complete=True)
        next_month = datetime(2026,12,1,0,tzinfo=timezone.utc)
        digital = deepcopy(self.rows[0]);digital['raw']['deliveryPlannedMoment'] = '2026-11-30 12:00:00'
        self.queue.sync_snapshot([digital], started_at=next_month-timedelta(days=1), complete=True)
        self.assertEqual([str(d['day']) for d in self.queue.archive(now=next_month)['days']],
                         ['2026-11-30','2026-11-04','2026-11-03'])
        restricted = self.queue.archive(now=next_month, accounts={'oz_acc2','wb_acc2'})
        self.assertEqual([str(d['day']) for d in restricted['days']], ['2026-11-04'])
        self.assertEqual([r['external_id'] for r in restricted['rows']], ['DISKVER'])
        hidden = self.queue.archive(now=next_month, day=NOW.date(), accounts={'oz_acc2','wb_acc2'})
        self.assertEqual(hidden['rows'], []);self.assertEqual(hidden['ready_batches'], [])
        # 21:00 UTC is the start of the next Moscow business day.
        before = datetime(2026,11,3,20,59,tzinfo=timezone.utc)
        after = before+timedelta(minutes=1)
        self.assertEqual(self.queue.archive(now=before)['days'], [])
        self.assertEqual(str(self.queue.archive(now=after)['days'][0]['day']), '2026-11-03')
        with self.assertRaises(ValueError): self.queue.archive(now=before, day=NOW.date())
        self.assertEqual(self.queue.archive(now=next_month, accounts=set())['days'], [])

    def test_archive_http_is_scoped_read_only_and_rejects_current_or_invalid_dates(self):
        from fastapi.testclient import TestClient
        from print_service.service import PrintService
        from web.print_app import create_app
        class NoNetwork:
            def __getattr__(self, name): raise AssertionError('provider_called:'+name)
        batch = self.reserve()
        self.queue.publish_pair(batch['id'], generation=1, pair_dir=self.prepare(batch))
        service = PrintService(queue=self.queue, clients=NoNetwork(), root=self.root,
                               clock=lambda:NOW+timedelta(days=1))
        with TestClient(create_app(lambda:service), headers={'X-Pult-User':'print'}) as client:
            data = client.get('/api/print/archive').json()
            self.assertEqual(len(data['ready_batches']), 1)
            self.assertEqual(data['days'][0]['printed_count'], 2)
            for day in ('2026-11-04','2026-11-05','not-a-date'):
                self.assertEqual(client.get('/api/print/archive', params={'day':day}).status_code, 422)
            scoped = client.get('/api/print/archive', headers={'X-Pult-User':'tolkachev'}).json()
            self.assertEqual(scoped['days'], []);self.assertEqual(scoped['rows'], [])
            digital = client.get('/api/print/archive', headers={'X-Pult-User':'bersenev'}).json()
            self.assertEqual(digital['days'][0]['printed_count'],2)
            self.assertEqual([b['id'] for b in digital['ready_batches']],[str(batch['id'])])
            self.assertEqual({r['account'] for r in digital['rows']},{'oz_acc1'})
            for path in ('/warehouse/print/archive','/api/print/archive'):
                self.assertEqual(client.get(path, headers={'X-Pult-User':'unknown'}).status_code, 403)
            response = client.get(f"/api/print/batches/{batch['id']}/files/labels")
            self.assertEqual(response.status_code, 200)
            page = client.get('/warehouse/print/archive', headers={'X-Pult-User':'tolkachev'})
            self.assertNotIn('Цифровой квадрат', page.text)
            self.assertIn('class="current" href="/warehouse/print/archive"', page.text)
        self.assertEqual(self.queue.batch(batch['id'])['generation'], 1)

    def test_external_print_confirmation_survives_sync_and_excludes_all_print_paths(self):
        oid=self.rows[0]['raw']['id'];evidence={'sha256':'a'*64,'source':'screenshot'}
        batch=self.queue.reserve_counter(self.counter,request_key='failed-one',now=NOW,order_ids=[oid])
        self.queue.fail_batch(batch['id'],generation=1,error='ozon_scanit_missing')
        result=self.queue.mark_external_printed([oid],accounts={'oz_acc1'},now=NOW,evidence=evidence)
        self.assertEqual(result,dict(marked=[oid],already_printed=0))
        repeat=self.queue.mark_external_printed([oid],accounts={'oz_acc1'},now=NOW,evidence=evidence)
        self.assertEqual(repeat,dict(marked=[],already_printed=1))
        self.queue.sync_snapshot(self.rows,started_at=NOW+timedelta(minutes=1),complete=True)
        view=self.queue.view(now=NOW+timedelta(minutes=1));row=next(r for r in view['rows'] if str(r['id'])==oid)
        self.assertTrue(row['printed']);self.assertTrue(row['printed_externally'])
        self.assertEqual(row['color'],'green');self.assertIsNone(row['batch_id'])
        self.assertFalse(row['receipt_ready']);self.assertEqual(view['ready_batches'],[])
        self.assertEqual(view['counters'][self.counter]['count'],1)
        self.assertEqual(view['external_printed_counts'],{'oz_acc1':1})
        self.assertEqual(view['jobs'],[])
        self.assertNotIn(oid,[str(r['id']) for r in self.queue.label_candidates(now=NOW)])
        with self.assertRaises(PrintBlocked):self.queue.retry_failed_batch(batch['id'],generation=1)
        with self.assertRaises(PrintBlocked):self.queue.reserve_counter(self.counter,request_key='single',now=NOW,order_ids=[oid])
        with self.assertRaises(PrintBlocked):self.queue.reserve_orders([oid],account='oz_acc1',request_key='file',now=NOW,title='Список')
        remaining=self.reserve('others')
        self.assertEqual([r['ms_order_id'] for r in remaining['orders']],[self.rows[1]['raw']['id']])

    def test_external_confirmation_scope_is_atomic_and_never_replaces_published_files(self):
        ids=[r['raw']['id'] for r in self.rows];evidence={'sha256':'a'*64}
        with self.assertRaises(PrintBlocked):self.queue.mark_external_printed(ids,accounts={'oz_acc2'},now=NOW,evidence=evidence)
        with self.assertRaises(PrintBlocked):self.queue.mark_external_printed(ids+[str(uuid4())],accounts={'oz_acc1'},now=NOW,evidence=evidence)
        self.assertEqual(self.queue.view(now=NOW)['external_printed_counts'],{})
        batch=self.reserve()
        with self.assertRaises(PrintBlocked):self.queue.mark_external_printed(ids,accounts={'oz_acc1'},now=NOW,evidence=evidence)
        self.queue.publish_pair(batch['id'],generation=1,pair_dir=self.prepare(batch))
        result=self.queue.mark_external_printed(ids,accounts={'oz_acc1'},now=NOW,evidence=evidence)
        self.assertEqual(result,dict(marked=[],already_printed=2))
        self.assertEqual(self.queue.view(now=NOW)['external_printed_counts'],{})
        self.assertTrue(self.queue.ready_files(batch['id'])['labels'].is_file())

    def test_external_confirmation_and_counter_click_are_serialized_without_duplicates(self):
        oid=self.rows[0]['raw']['id']
        def mark():
            try:return self.queue.mark_external_printed([oid],accounts={'oz_acc1'},now=NOW,evidence={'sha256':'b'*64})
            except PrintBlocked as exc:
                self.assertEqual(str(exc),'counter_already_reserved');return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            manual=pool.submit(mark);counter=pool.submit(self.reserve,'simultaneous')
            result=manual.result();batch=counter.result()
        members={r['ms_order_id'] for r in batch['orders']}
        self.assertEqual(oid in members,result is None)
        self.assertEqual(len(members)+(1 if result else 0),2)

    def test_external_confirmation_archive_and_http_never_invent_ready_files(self):
        from fastapi.testclient import TestClient
        from print_service.service import PrintService
        from web.print_app import create_app
        oid=self.rows[0]['raw']['id']
        self.queue.mark_external_printed([oid],accounts={'oz_acc1'},now=NOW,evidence={'sha256':'c'*64})
        class NoNetwork:
            def __getattr__(self,name):raise AssertionError('provider_called:'+name)
        service=PrintService(queue=self.queue,clients=NoNetwork(),root=self.root,clock=lambda:NOW)
        with TestClient(create_app(lambda:service),headers={'X-Pult-User':'bersenev'}) as client:
            data=client.get('/api/print').json();self.assertEqual(data['external_printed_counts'],{'oz_acc1':1})
            self.assertEqual(client.post(f'/api/print/orders/{oid}/pair',json={'request_key':str(uuid4())}).status_code,409)
            self.assertEqual(client.get(f'/api/print/orders/{oid}/files/receipts').status_code,409)
            foreign=client.get('/api/print',headers={'X-Pult-User':'tolkachev'}).json()
            self.assertEqual(foreign['external_printed_counts'],{})
        archive=self.queue.archive(now=NOW+timedelta(days=1))
        self.assertEqual(archive['days'][0]['printed_count'],1)
        self.assertEqual(archive['external_printed_counts'],{'oz_acc1':1})
        row=next(r for r in archive['rows'] if str(r['id'])==oid)
        self.assertTrue(row['printed']);self.assertTrue(row['printed_externally'])
        self.assertFalse(row['receipt_ready']);self.assertIsNone(row['batch_id'])
        self.assertEqual(archive['ready_batches'],[])
        self.assertEqual(self.queue.archive(now=NOW+timedelta(days=1),accounts={'oz_acc2'})['days'],[])

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
