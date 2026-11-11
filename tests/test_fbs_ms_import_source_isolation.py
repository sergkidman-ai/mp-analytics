# поток: inv
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from ops import fbs_ms_import_scheduler as scheduler, fbs_ms_source_scan as scanner

ROW={'number':'123-0001-1','platform':'ozon','account':'oz_acc2','status':'awaiting_packaging',
     'created_at':'2026-11-05T04:25:52+00:00','products':[{'code':'0001','quantity':1}]}
WINDOW=['2026-11-05T00:00:00+00:00','2026-11-05T06:00:00+00:00']

class IsolationTests(unittest.TestCase):
    def test_failed_market_keeps_successful_feed_after_retries(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            def scan():
                (root/'source_scan.json').write_text(json.dumps({'window_utc':WINDOW,
                    'accounts':{'oz_acc2':[ROW]},'errors':{'ya_acc1':'HTTP500'}}))
                return {'errors':{'ya_acc1':'HTTP500'}}
            with patch.object(scheduler,'OUT',root), \
                 patch.object(scheduler,'cursor',return_value=datetime(2026,11,5,2,tzinfo=timezone.utc)), \
                 patch.object(scanner,'scan',side_effect=scan), \
                 patch.object(scanner,'yandex',side_effect=RuntimeError('HTTP500')) as fetch, \
                 patch.object(scheduler.time,'sleep'):
                rows,_,_=scheduler.source_rows()
            self.assertEqual(rows,[ROW]);self.assertEqual(fetch.call_count,2)
            self.assertIn('ya_acc1',json.loads((root/'source_scan.json').read_text())['errors'])

    def test_create_other_account_hold_cursor_then_recover_without_duplicate(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'source_scan.json').write_text(json.dumps({'errors':{'ya_acc1':'HTTP500'}}))
            with patch.object(scheduler,'OUT',root), \
                 patch.object(scheduler,'RUNTIME',root/'schedule'), \
                 patch.object(scheduler,'LOCK',root/'schedule/lock'), \
                 patch.object(scheduler,'KNOWN',root/'schedule/known.json'), \
                 patch.object(scheduler,'STATE',root/'schedule/state.json'), \
                 patch.object(scheduler,'source_rows',return_value=([ROW],*WINDOW)), \
                 patch.object(scheduler,'exact_ms_numbers',return_value=set()), \
                 patch.object(scheduler,'prices_for',return_value=({},'digest')), \
                 patch.object(scheduler,'enter_digest',return_value='digest'), \
                 patch.object(scheduler,'find_customerorder',return_value=None), \
                 patch('ops.fbs_ms_import_next5.create_one',return_value={'number':ROW['number'],'result':'created'}) as create:
                first=scheduler.run(True)
                self.assertEqual(len(first['created']),1);self.assertFalse(first['cursor_advanced'])
                self.assertFalse((root/'schedule/state.json').exists())
                self.assertEqual(first['errors'][0]['account'],'ya_acc1')
                (root/'source_scan.json').write_text(json.dumps({'errors':{}}))
                second=scheduler.run(True)
                self.assertTrue(second['cursor_advanced']);self.assertEqual(create.call_count,1)
                self.assertEqual(second['eligible'],0)
