# поток: ret
"""Late QR delivery uses original PNG, shared receipts and isolated synthetic cache."""
from contextlib import ExitStack
import base64
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import qrcode
from fastapi.testclient import TestClient
from returns_bot import wb_qr, wb_qr_delivery as delivery, wb_qr_followup as followup
from returns_bot import daily_push, wb_qr_ingest, tg


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.enterContext = self.stack.enter_context
        self.addCleanup(self.stack.close)
        self.enterContext(patch.object(tg, 'codes_only_notify_ids', return_value=[]))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.now = datetime(2026, 10, 26, 15, tzinfo=wb_qr.MSK)
        for obj, attr, value in [(wb_qr, 'CACHE', root/'diskver'),
                                  (wb_qr, 'CACHE_DIGITAL', root/'digital'),
                                  (delivery, 'DIRECTORY', root/'receipts')]:
            self.enterContext(patch.object(obj, attr, value))
        self.clock = self.enterContext(patch.object(wb_qr, 'now_msk', return_value=self.now))
        self.enterContext(patch.object(tg, 'notify_ids', return_value=['fixture1', 'fixture2']))
        self.doc = self.enterContext(patch.object(tg, 'send_document', return_value={'message_id': 42}))
        buf = io.BytesIO()
        qrcode.make('unit-fixture-no-live-access').save(buf, format='PNG')
        self.png = buf.getvalue()
        self.validity = 'Коды действуют только 26 октября до 00:00 по мск'
        wb_qr.store(self.png, '2026-10-26', self.validity)

    def test_late_qr_sent_once_to_each_user_with_durable_receipts(self):
        self.assertEqual(followup.run(dry_run=True), {'pending': 2})
        self.doc.assert_not_called()
        self.assertFalse(delivery.DIRECTORY.exists())
        self.assertEqual(followup.run(), {'sent': 2})
        self.assertEqual(followup.run(), {'skipped': 2})
        self.assertEqual(self.doc.call_count, 2)
        self.assertEqual(self.doc.call_args.args[1], self.png)
        self.assertEqual(self.doc.call_args.kwargs['tries'], 1)
        self.assertTrue(all(x['message_id'] == 42 for x in delivery.read('2026-10-26').values()))

    def test_report_and_followup_share_receipts(self):
        self.enterContext(patch.object(daily_push.render, 'summaries', return_value=[('Дисквэр','summary')]))
        self.enterContext(patch.object(daily_push, 'barcode_photos', return_value=[]))
        send = self.enterContext(patch.object(tg, 'send'))
        self.assertEqual(daily_push.run(['fixture1','fixture2'], skip_collect=True), 0)
        self.assertEqual(followup.run(), {'skipped':2})
        self.assertEqual(self.doc.call_count, 2)
        self.assertEqual(send.call_count, 2)

    def test_explicit_rejection_retries_only_missing_recipient(self):
        self.doc.side_effect = [{'message_id':1}, tg.Rejected('429')]
        self.assertEqual(followup.run(), {'sent':1, 'failed':1})
        self.doc.side_effect = None
        self.assertEqual(followup.run(), {'skipped':1, 'sent':1})
        self.assertEqual(self.doc.call_count, 3)

    def test_ambiguous_timeout_is_not_blindly_duplicated(self):
        self.doc.side_effect = RuntimeError('transport failure')
        self.assertEqual(followup.run(), {'uncertain':2})
        self.assertEqual(followup.run(), {'uncertain':2})
        self.assertEqual(self.doc.call_count, 2)

    def test_connect_timeout_is_safe_to_retry_but_read_timeout_is_uncertain(self):
        import requests
        with patch.object(tg.requests,'post',side_effect=requests.ConnectTimeout('fixture')):
            with self.assertRaises(tg.Rejected):
                tg._api('sendDocument',tries=1)
        with patch.object(tg.requests,'post',side_effect=requests.ReadTimeout('fixture')):
            with self.assertRaises(RuntimeError) as raised:
                tg._api('sendDocument',tries=1)
            self.assertNotIsInstance(raised.exception,tg.Rejected)

    def test_pending_intent_survives_process_crash(self):
        with delivery.locked():
            wb_qr.atomic(delivery.DIRECTORY/'2026-10-26.json', json.dumps({
                'wb_acc2:fixture1':{'status':'sending'}}).encode())
        self.assertEqual(followup.run(), {'uncertain':1,'sent':1})
        self.assertEqual(self.doc.call_args.args[0], 'fixture2')

    def test_no_followup_before_report_or_on_sunday_or_for_yesterday(self):
        self.assertEqual(followup.run(now=self.now.replace(hour=14,minute=14)), {})
        self.assertEqual(followup.run(now=self.now.replace(day=1,month=11)), {})
        self.assertEqual(followup.run(now=self.now.replace(day=27)), {'missing':1})
        self.doc.assert_not_called()

    def test_two_companies_are_independent(self):
        wb_qr.store(self.png,'2026-10-26',self.validity,account='wb_acc1')
        self.assertEqual(followup.run(), {'sent':4})
        self.assertEqual(followup.run(), {'skipped':4})

    def test_successful_upload_triggers_late_delivery_and_repeated_upload_is_safe(self):
        self.enterContext(patch.object(wb_qr_ingest,'token',return_value='unit-upload-token'))
        client=TestClient(wb_qr_ingest.app)
        body={'png_b64':base64.b64encode(self.png).decode(),'day':'2026-10-26','validity':self.validity}
        for _ in range(2):
            response=client.post(wb_qr_ingest.UPLOAD,json=body,headers={'Authorization':'Bearer unit-upload-token'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.doc.call_count,2)

    def test_interleaved_senders_cannot_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:followup.run(), range(2)))
        self.assertEqual(sum(x.get('sent',0) for x in results),2)
        self.assertEqual(self.doc.call_count,2)


class HourlyWorkerTests(unittest.TestCase):
    def test_real_worker_hourly_recovery_with_isolated_chrome_api(self):
        import subprocess
        result=subprocess.run(['node',str(Path(__file__).with_name('test_wb_qr_hourly_background.js'))],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__': unittest.main()
