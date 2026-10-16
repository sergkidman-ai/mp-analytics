# поток: infra
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from tools import infra_ms_recovery_20261016 as job


class RecoveryTests(unittest.TestCase):
    def test_overlap_uses_one_body_and_keeps_both_attribute_changes(self):
        before = {'id': 'p', 'weight': 1, 'attributes': [
            {'id': 'd', 'value': 'old-dim'}, {'id': 'w', 'value': 'old-wb'}]}
        original = deepcopy(before)
        dimbody = {'id': 'p', 'weight': 2, 'attributes': [
            {'id': 'd', 'value': 'new-dim'}, {'id': 'w', 'value': 'old-wb'}]}
        def wbpatch(product, *args):
            self.assertEqual(product['weight'], 2)
            self.assertEqual(job.attrs(product)['d'], 'new-dim')
            product['attributes'][1]['value'] = 'new-wb'
            return {'id': 'p', 'attributes': product['attributes']}
        with patch.object(job.dims, 'patch', return_value=dimbody), patch.object(job.wb, 'patch', side_effect=wbpatch):
            body = job.combine(before, {'id':'p', 'wb':{'x':1}, 'dimensions':{'x':1}}, {}, 'w', set())
        self.assertEqual(body['weight'], 2)
        self.assertEqual(job.attrs(body), {'d':'new-dim', 'w':'new-wb'})
        self.assertEqual(before, original)

    def test_noop_does_not_generate_write(self):
        with patch.object(job.dims,'patch',return_value=None):
            self.assertIsNone(job.combine({'id':'p'}, {'id':'p','dimensions':{'x':1}},None,'w',set()))

    def test_verify_accepts_only_exact_expected_combination(self):
        before = {'id':'p','weight':1,'name':'same','attributes':[{'id':'w','value':'old'}]}
        body = {'id':'p','weight':2,'attributes':[{'meta':{'href':'/attrs/w'},'value':'new'}]}
        after = dict(before, **body, updated='server timestamp')
        job.verify(before,after,body)
        with self.assertRaises(RuntimeError):job.verify(before,dict(after,name='unexpected'),body)

    def test_verify_rejects_loss_of_unrelated_attribute(self):
        before = {'id':'p','attributes':[{'id':'a','value':5}]}
        with self.assertRaises(RuntimeError):job.verify(before,{'id':'p','attributes':[]},{'id':'p'})

    def test_scope_has_exact_original_union_and_pilot_first(self):
        rows = [{'id':str(i),'wb':{'x':1}} for i in range(200)]
        for row in rows[:3]:row['dimensions']={'x':1}
        rows += [{'id':str(i),'dimensions':{'x':1}} for i in range(200,802)]
        self.assertEqual(len(job.scope({'items':rows})),802)
        rows[-1]['id']='0'
        with self.assertRaises(RuntimeError):job.scope({'items':rows})

    def test_time_window_and_space_guard_fail_closed(self):
        with patch.object(job.shutil,'disk_usage',return_value=SimpleNamespace(free=job.MIN_FREE)), \
             patch.object(job.wb,'unit_inventory',return_value=[]), \
             patch.object(job.wb,'active_writers',return_value=[]), patch.object(job,'timer_conflicts'):
            job.guard(now=job.START)
            with self.assertRaises(RuntimeError):job.guard(now=job.START-timedelta(seconds=1))
            with self.assertRaises(RuntimeError):job.guard(now=job.WRITE_END)
            job.guard(False,now=job.WRITE_END)
            with self.assertRaises(RuntimeError):job.guard(False,now=job.END)
        with patch.object(job.shutil,'disk_usage',return_value=SimpleNamespace(free=job.MIN_FREE-1)):
            with self.assertRaises(RuntimeError):job.guard(now=job.START)

    def test_wb_request_never_suppresses_webhook_but_dimensions_keeps_original_mode(self):
        response=MagicMock()
        response.__enter__.return_value=response
        response.headers={}
        response.read.return_value=json.dumps([{'id':'p'}]).encode()
        with patch.object(job.urllib.request,'urlopen',return_value=response) as send:
            job.send_once([{'id':'p'}],True,'exact-prefix')
            h={k.lower():v for k,v in send.call_args.args[0].header_items()}
            self.assertNotIn(job.dims.previous.HEADER.lower(),h)
            job.send_once([{'id':'p'}],False,'exact-prefix')
            h={k.lower():v for k,v in send.call_args.args[0].header_items()}
            self.assertEqual(h[job.dims.previous.HEADER.lower()],'exact-prefix')

    def test_uncertain_write_is_never_retried(self):
        with patch.object(job.urllib.request,'urlopen',side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError):job.send_once([{'id':'p'}],True,'x')
            self.assertEqual(send.call_count,1)

    def test_response_must_include_exact_sent_ids(self):
        response=MagicMock();response.__enter__.return_value=response;response.headers={}
        response.read.return_value=b'[{"id":"wrong"}]'
        with patch.object(job.urllib.request,'urlopen',return_value=response):
            with self.assertRaises(RuntimeError):job.send_once([{'id':'p'}],True,'x')


if __name__ == '__main__':
    unittest.main()
