# поток: print
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock,MagicMock,patch
from uuid import uuid4
import json
import time

from ops import ms_market_status_live as live,ms_market_status_state as state,ms_market_status_urgent as urgent
from tests import test_ms_market_status_live as fixture


class UrgentCancellationTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(patch.stopall)
        patch.object(state,'RUNTIME',Path(self.tmp.name)).start()
        patch.object(live,'now',return_value=fixture.NOW).start()
        patch.object(state,'reserve_slot',return_value=0).start()
        patch.object(live,'status_ids',return_value={'Отменен - сроки':'cancelled'}).start()
        self.doc=fixture.document(uid=str(uuid4()),name='0161451251-0164-1',status='Собирается')
        state.index([live.row_for(self.doc)])
        self.get=patch.object(live.ms_api,'get').start()
        self.put=patch.object(live.requests,'put',return_value=Mock(status_code=200,json=lambda:{})).start()
        self.provider=patch.object(live,'Provider').start()
        self.proof=fixture.proof('cancelled',number=self.doc['name'],ms_id=self.doc['id'])
        self.provider.return_value.fetch.return_value={self.doc['id']:self.proof}

    def request(self):
        return urgent.request_cancel(self.doc['id'],'oz_acc1',self.doc['name'],dict(posting_number=self.doc['name'],status='cancelled'))

    def target(self):
        value=deepcopy(self.doc);value['state']['name']='Отменен - сроки';return value

    def test_exact_cancel_is_reverified_and_only_order_state_is_written(self):
        self.request();self.get.side_effect=[self.doc,self.target()]
        result=urgent.step()
        self.assertEqual(result['result'],'updated');self.put.assert_called_once()
        self.assertEqual(set(self.put.call_args.kwargs['json']),{'state'})
        self.provider.return_value.fetch.assert_called_once()
        self.assertIsNone(urgent.step())

    def test_known_429_is_pending_and_retry_has_a_new_attempt_id(self):
        self.request();self.get.side_effect=[self.doc,self.doc]
        self.put.return_value=Mock(status_code=429,headers={'X-Lognex-Retry-After':'4000'},json=lambda:{})
        first=urgent.step();self.assertEqual(first['result'],'rate_limited');self.assertGreaterEqual(first['retry_after'],4)
        with state.database() as con:
            self.assertEqual(con.execute('SELECT state FROM jobs').fetchone()[0],'pending')
            first_id=con.execute('SELECT id FROM attempts').fetchone()[0]
        self.put.return_value=Mock(status_code=200,json=lambda:{})
        self.get.side_effect=[self.doc,self.target()]
        with patch.object(state.time,'time',return_value=time.time()+10):second=urgent.step()
        self.assertEqual(second['result'],'updated');self.assertEqual(self.put.call_count,2)
        with state.database() as con:
            rows=con.execute('SELECT id,outcome FROM attempts').fetchall()
        self.assertEqual(len(rows),2);self.assertEqual({r['outcome'] for r in rows},{'rejected','confirmed'})
        self.assertEqual(len({r['id'] for r in rows}),2)

    def test_legacy_explicit_429_is_reconciled_but_unknown_or_staff_changes_stay_blocked(self):
        attempt,created=state.begin_attempt(self.doc,'Отменен - сроки',self.proof,None)
        self.assertTrue(created);state.end_attempt(attempt['id'],'uncertain',{'http':429})
        state.enqueue(self.proof,'fast',force=True);old=state.claim();state.finish(old,{'result':'uncertain'},uncertain=True)
        self.request();self.get.side_effect=[self.doc,self.target()]
        self.assertEqual(urgent.step()['result'],'updated');self.assertEqual(self.put.call_count,1)
        with state.database() as con:self.assertEqual(con.execute('SELECT outcome FROM attempts WHERE id=?',(attempt['id'],)).fetchone()[0],'rejected')

    def test_unknown_put_is_never_repeated(self):
        attempt,_=state.begin_attempt(self.doc,'Отменен - сроки',self.proof,None)
        state.end_attempt(attempt['id'],'uncertain',{'transport_error':'ReadTimeout'})
        self.request();self.get.return_value=self.doc
        self.assertEqual(urgent.step()['result'],'uncertain');self.put.assert_not_called()

    def test_market_retraction_and_manual_ms_state_prevent_write(self):
        self.request();self.get.return_value=self.doc
        self.provider.return_value.fetch.return_value={self.doc['id']:dict(self.proof,status='other')}
        self.assertEqual(urgent.step()['result'],'cancellation_not_confirmed');self.put.assert_not_called()
        self.provider.return_value.fetch.return_value={self.doc['id']:self.proof}
        self.request();changed=deepcopy(self.doc);changed['state']['name']='Подтвержден';self.get.return_value=changed
        self.assertEqual(urgent.step()['result'],'changed_by_other');self.put.assert_not_called()

    def test_invalid_proof_and_other_workers_cannot_claim_urgent_job(self):
        with self.assertRaises(ValueError):urgent.request_cancel(self.doc['id'],'oz_acc1',self.doc['name'],{'posting_number':self.doc['name'],'status':'awaiting_deliver'})
        self.request();self.assertIsNone(state.claim('fast'));self.assertIsNone(state.claim('night'))
        self.assertIsNotNone(state.claim('urgent'))

    def test_completed_urgent_job_does_not_capture_later_regular_status_work(self):
        self.request();job=state.claim('urgent');state.finish(job,{'result':'updated'})
        state.enqueue(dict(self.proof,fetched_at='2030-01-01T00:00:00+00:00'),'fast',force=True)
        self.assertIsNone(state.claim('urgent'));self.assertIsNotNone(state.claim('fast'))

    def test_print_sync_waits_for_current_collection_to_finish(self):
        state.set_setting('urgent_print_sync_needed',True)
        queue=MagicMock();cursor=queue.transaction.return_value.__enter__.return_value
        cursor.fetchone.return_value={'running':True}
        self.assertFalse(urgent.flush_print_sync(queue));queue.enqueue_sync.assert_not_called()
        cursor.fetchone.return_value=None
        self.assertTrue(urgent.flush_print_sync(queue));queue.enqueue_sync.assert_called_once()
        self.assertFalse(state.setting('urgent_print_sync_needed'))
