# поток: inv
from contextlib import nullcontext
from datetime import datetime,timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock,patch
import unittest
from customer_guard import recovery_events as events
from customer_guard import service

EVENT={'order_id':1,'check_id':2,'platform':'ozon','account':'oz_acc2','number':'123-0001-1'}

class EventTests(unittest.TestCase):
    def test_only_current_confirmed_missing_publishes(self):
        with TemporaryDirectory() as tmp,patch.object(events,'EVENT_ROOT',Path(tmp)):
            with patch.object(events.db,'query',return_value=[]):self.assertFalse(events.enqueue_missing(1))
            self.assertFalse(events.event_files())
            with patch.object(events.db,'query',return_value=[EVENT]):self.assertTrue(events.enqueue_missing(1))
            self.assertEqual(json.loads(events.event_files()[0].read_text()),EVENT)
            self.assertTrue((Path(tmp)/'pending').exists())
            self.assertTrue(events.sync_pending({}))  # A new inbox event must keep the marker.

    def test_empty_queue_clears_boot_resume_marker(self):
        with TemporaryDirectory() as tmp,patch.object(events,'EVENT_ROOT',Path(tmp)):
            events.publish(EVENT)
            for path in events.event_files():path.unlink()
            self.assertTrue(events.sync_pending({'key':{'pending_event':True}}))
            self.assertFalse(events.sync_pending({}))
            self.assertFalse((Path(tmp)/'pending').exists())

    def test_guard_publishes_after_commit_even_without_telegram(self):
        for committed in [True,False]:
            order={'account':'oz_acc2','posting_number':'123-0001-1'}
            job={'order_id':1,'order':order}
            client=Mock();client.find_order.return_value=None;client.requests=1
            sequence=[]
            def finish(*args):sequence.append('commit');return committed
            with patch.object(service,'exclusive',return_value=nullcontext(lambda:None)), \
                 patch.object(service.db,'query',return_value=[{}]), \
                 patch.object(service.store,'claim',return_value=job), \
                 patch.object(service.store,'refresh',return_value=True), \
                 patch.object(service.store,'fail'), \
                 patch.object(service,'shipment_rule',return_value={}), \
                 patch.object(service,'evaluate',return_value={'signal':'missing_ms'}), \
                 patch.object(service.store,'finish',side_effect=finish), \
                 patch.object(service,'enqueue_missing',side_effect=lambda _:sequence.append('publish')) as publish:
                result=service.work(limit=1,budget=10,client=client,notify=False,
                    get=lambda _: {'status':'awaiting_packaging','posting_number':'123-0001-1'},clock=lambda:datetime.now(timezone.utc))
            self.assertEqual(sequence,['commit','publish'] if committed else ['commit'])
            self.assertEqual(result['completed'],int(committed))

    def test_publish_failure_does_not_undo_completed_guard_check(self):
        job={'order_id':1,'order':{'account':'oz_acc2','posting_number':'123-0001-1'}}
        client=Mock();client.find_order.return_value=None;client.requests=1
        with patch.object(service,'exclusive',return_value=nullcontext(lambda:None)), \
             patch.object(service.db,'query',return_value=[{}]), \
             patch.object(service.store,'claim',return_value=job), \
             patch.object(service.store,'refresh',return_value=True), \
             patch.object(service.store,'fail'), \
             patch.object(service,'shipment_rule',return_value={}), \
             patch.object(service,'evaluate',return_value={'signal':'missing_ms'}), \
             patch.object(service.store,'finish',return_value=True), \
             patch.object(service,'enqueue_missing',side_effect=OSError('disk')):
            result=service.work(limit=1,budget=10,client=client,notify=False,
                get=lambda _: {'status':'awaiting_packaging','posting_number':'123-0001-1'})
        self.assertEqual(result['completed'],1)
        self.assertEqual(result['recovery_errors'],['OSError'])
