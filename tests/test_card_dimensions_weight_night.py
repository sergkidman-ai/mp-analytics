# поток: card
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from tools import card_dimensions_weight_night as m


class CombinedNightTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.out=Path(temp.name); self.mock(m,'OUT',self.out)
        self.row={'id':'p','article':'art','external_code':'1234','supplier_id':'s','name':'Картридж',
                  'dimensions':[{'id':str(i),'value':str(i+10)} for i in range(3)],
                  'weight_before':'1.2','weight_target':'0.9'}
        self.live={'id':'p','article':'art','externalCode':'1234','name':'Картридж',
                   'supplier':{'meta':{'href':'https://example.test/entity/counterparty/s'}},
                   'attributes':[{'id':'other','value':'keep'}],'weight':1.2,'barcodes':[{'ean8':'12345678'}]}

    def mock(self,obj,name,value):
        ctx=patch.object(obj,name,value);ctx.start();self.addCleanup(ctx.stop);return value

    def allow_send(self):
        self.mock(m,'START',datetime(2020,1,1,tzinfo=timezone.utc))
        self.mock(m,'END',datetime(2100,1,1,tzinfo=timezone.utc))
        self.mock(m.ms_api,'TOKEN','dummy')
        self.mock(m,'check_disk',lambda:None)

    def test_combines_dimensions_weight_and_preserves_other_attributes(self):
        before=copy.deepcopy(self.live);body=m.patch(self.live,self.row,set())
        self.assertEqual(set(body),{'id','attributes','weight'})
        self.assertEqual(body['weight'],.9)
        self.assertEqual(body['attributes'][0],before['attributes'][0])
        self.assertEqual(self.live,before)

    def test_weight_only_does_not_send_attributes(self):
        self.row['dimensions']=[]
        self.assertEqual(m.patch(self.live,self.row,set()),{'id':'p','weight':.9})

    def test_dimensions_only_does_not_send_weight(self):
        del self.row['weight_target']
        self.assertNotIn('weight',m.patch(self.live,self.row,set()))

    def test_weight_drift_blocks_both_changes(self):
        self.live['weight']=1.1
        with self.assertRaisesRegex(RuntimeError,'WEIGHT_CHANGED'):
            m.patch(self.live,self.row,set())

    def test_already_desired_all_noop(self):
        self.live['weight']=.9;self.live['attributes']+=self.row['dimensions']
        self.assertIsNone(m.patch(self.live,self.row,set()))

    def test_partial_dimensions_do_not_overwrite(self):
        self.live['attributes'].append(self.row['dimensions'][0])
        with self.assertRaisesRegex(RuntimeError,'NO_LONGER_ALL_EMPTY'):
            m.patch(self.live,self.row,set())

    def test_fill_zero_weight(self):
        self.live['weight']=0;self.row['weight_before']='0'
        self.assertEqual(m.patch(self.live,self.row,set())['weight'],.9)

    def test_readback_checks_weight_and_preserves_barcode(self):
        body=m.patch(self.live,self.row,set());after={**self.live,**body}
        m.verify(after,body,self.live)
        after['weight']=1
        with self.assertRaisesRegex(RuntimeError,'Weight readback'):m.verify(after,body,self.live)
        after['weight']=.9;after['barcodes']=[]
        with self.assertRaisesRegex(RuntimeError,'barcodes'):m.verify(after,body,self.live)

    def test_manual_override_is_narrow_and_explicit(self):
        self.mock(m.previous,'source_gate',lambda *a:None)
        self.mock(m,'fresh_rapid',lambda:{'price':[]})
        row={'id':m.MANUAL_ID,'external_code':'6243','article':'I-B100_SET',
             'manual_weight_override':True,'weight_target':'0.48'}
        good,skip=m.check_sources([row],{})
        self.assertEqual(good,[row]);self.assertFalse(skip)
        row['weight_target']='4.8'
        with self.assertRaisesRegex(RuntimeError,'Manual override'):m.check_sources([row],{})

    def test_source_weight_drift_skips(self):
        self.mock(m.previous,'source_gate',lambda *a:None)
        self.mock(m,'fresh_rapid',lambda:{'price':[{'CodeID':'art','Weight':'1'}]})
        row={**self.row,'dimensions':[],'supplier':'Рапид / Суперфайн'}
        good,skip=m.check_sources([row],{})
        self.assertFalse(good);self.assertEqual(skip[0]['id'],'p')

    def test_low_disk_blocks_post(self):
        self.mock(m.shutil,'disk_usage',lambda _:Mock(free=m.MIN_FREE-1))
        post=self.mock(m.requests,'post',Mock())
        with self.assertRaisesRegex(RuntimeError,'disk space'):m.send([{'id':'p'}],'https://example.test/hook')
        post.assert_not_called();self.assertFalse((self.out/'journal.jsonl').exists())

    def test_header_is_request_scoped_and_secret_not_logged(self):
        self.allow_send();post=self.mock(m.requests,'post',Mock(return_value=Mock(status_code=200,json=lambda:[{'id':'p'}])))
        prefix='https://example.test/private-secret'
        m.send([{'id':'p'}],prefix)
        self.assertEqual(post.call_args.kwargs['headers'][m.previous.HEADER],prefix)
        self.assertNotIn(prefix,(self.out/'journal.jsonl').read_text())

    def test_uncertain_post_never_retried(self):
        self.allow_send();post=self.mock(m.requests,'post',Mock(side_effect=m.requests.Timeout()))
        with self.assertRaisesRegex(RuntimeError,'no automatic retry'):m.send([{'id':'p'}],'https://example.test/hook')
        self.assertEqual(post.call_count,1)
        self.assertEqual(json.loads((self.out/'journal.jsonl').read_text())['ids'],['p'])

    def test_daytime_blocks_post(self):
        self.mock(m,'START',datetime(2100,1,1,tzinfo=timezone.utc));self.mock(m,'check_disk',lambda:None)
        post=self.mock(m.requests,'post',Mock())
        with self.assertRaisesRegex(RuntimeError,'night window'):m.send([{'id':'p'}],'https://example.test/hook')
        post.assert_not_called()

    def test_existing_journal_blocks_rerun(self):
        (self.out/'authorization.json').write_text(json.dumps({'authorized':True,'notification_mode':m.previous.MODE}))
        (self.out/'summary.json').write_text('{}');(self.out/'journal.jsonl').write_text('uncertain')
        with self.assertRaisesRegex(RuntimeError,'Existing journal'):m.apply()


if __name__=='__main__':unittest.main()
