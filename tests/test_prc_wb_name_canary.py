# поток: prc
"""Critical boundaries for the scheduled, write-capable 200-card pilot."""
import copy
from datetime import timedelta
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock
import urllib.error

from tools.prc import wb_name_canary as job
from tools.prc.wb_fill import compose_details
from tools.prc.wb_name_preflight import digest


class Guards(unittest.TestCase):
    def test_only_requested_date_and_time(self):
        job.guard_window(job.START, True)
        for t in (job.START-timedelta(seconds=1), job.WRITE_END, job.START+timedelta(days=1)):
            with self.subTest(t=t), self.assertRaises(RuntimeError):
                job.guard_window(t, True)
        job.guard_window(job.WRITE_END, False)
        with self.assertRaises(RuntimeError):job.guard_window(job.END, False)

    def test_dimensions_and_weight_oneshot_is_seen_while_starting(self):
        units=[{'unit':'card-dimensions-weight-20261015.service','active':'activating'},
               {'unit':'card-own-dimensions-20261015.service','active':'inactive'},
               {'unit':'prc-wb-name-canary-20261015.service','active':'activating'}]
        self.assertEqual(job.active_writers(units), ['card-dimensions-weight-20261015.service'])

    def test_later_dimensions_timer_does_not_overlap(self):
        row={'unit':'card-dimensions-weight-20261015.timer',
             'next':int((job.START+timedelta(hours=2,minutes=20)).timestamp()*1_000_000)}
        self.assertEqual(len(job.check_timer_conflicts([row])),1)
        row['next']=int(job.START.timestamp()*1_000_000)
        with self.assertRaisesRegex(RuntimeError,'Overlapping'):job.check_timer_conflicts([row])

    def test_dimensions_lock_blocks_our_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);other=root/'dimensions.lock';other.touch()
            out=root/'out';out.mkdir()
            with other.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with mock.object(job,'DIMENSION_LOCK',other),mock.object(job,'OUT',out), \
                     mock.object(job,'check_files') as checked,mock('sys.argv',['job','--check']), \
                     mock('builtins.print'):
                    self.assertEqual(job.main(),1)
                    checked.assert_not_called()

    def test_frozen_scope_cannot_grow_to_201(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'plan.json').write_text(json.dumps({'items':[{'ms_id':str(i)} for i in range(201)]}))
            (root/'authorization.json').write_text(json.dumps({'authorized':True,
                'notification_mode':'all_webhooks_enabled','plan_sha256':job.sha(root/'plan.json'),
                'code_sha256':{}}))
            with mock.object(job,'OUT',root),self.assertRaisesRegex(RuntimeError,'Scope'):
                job.check_files()

    def test_uncertain_write_is_attempted_once_and_webhooks_are_not_disabled(self):
        with mock.object(job.ms_api,'TOKEN','test-only'),mock.object(job.urllib.request,'urlopen',
                   side_effect=urllib.error.URLError('timeout')) as request:
            with self.assertRaises(urllib.error.URLError):job.send_once([{'id':'p'}])
        self.assertEqual(request.call_count,1)
        headers={k.lower():v for k,v in request.call_args.args[0].header_items()}
        self.assertNotIn('x-lognex-webhook-disablebyprefix',headers)


class ProductSafety(unittest.TestCase):
    def setUp(self):
        self.raw={'consumable_type':'Картридж','title':'Q2612A','printer_models':[{'brand':'HP'}],
                  'ink_colors':['черный'],'chip':'С чипом'}
        self.name=compose_details(self.raw,'Картридж HP Q2612A с чипом').name
        self.aid='wb-id'
        self.p={'id':'p','code':'1234ep','externalCode':'1234','name':'Картридж HP Q2612A с чипом',
                'article':'A','supplier':{'meta':{'href':'s'}},'productFolder':{'meta':{'href':'f'}},
                'description':'Описание','uom':{'meta':{'href':'u'}},'weight':1,
                'barcodes':[{'code128':'ABC'}], 'attributes':[
                    {'id':'other','name':'Размер','type':'double','value':25},
                    {'id':self.aid,'name':'Название WB','type':'text','value':'Старое имя'}]}
        self.row={'ms_id':'p','code':'1234ep','external_code':'1234','supplier_name':self.p['name'],
                  'article':'A','supplier':self.p['supplier'],'old':'Старое имя','new':self.name,
                  'tc_semantic_sha256':digest(self.raw)}

    def test_only_wb_name_changes_and_payload_contains_no_weight_or_size_top_fields(self):
        before=copy.deepcopy(self.p)
        body=job.patch(self.p,self.row,self.raw,self.aid,set())
        self.assertEqual(set(body),{'id','attributes'})
        self.assertEqual(body['attributes'][0],self.p['attributes'][0])
        self.assertEqual(self.p,before)

    def test_manual_name_change_is_not_overwritten(self):
        self.p['attributes'][1]['value']='Правка человека'
        with self.assertRaisesRegex(RuntimeError,'WB name changed'):
            job.patch(self.p,self.row,self.raw,self.aid,set())

    def test_changed_supplier_article_or_model_is_not_written(self):
        self.p['article']='OTHER'
        with self.assertRaisesRegex(RuntimeError,'identity'):
            job.patch(self.p,self.row,self.raw,self.aid,set())

    def test_changed_tc_source_is_not_written(self):
        with self.assertRaisesRegex(RuntimeError,'source changed'):
            job.patch(self.p,self.row,dict(self.raw,chip='Без чипа'),self.aid,set())

    def test_existing_new_value_is_noop(self):
        self.p['attributes'][1]['value']=self.name
        self.assertIsNone(job.patch(self.p,self.row,self.raw,self.aid,set()))

    def test_verification_rejects_any_dimension_or_weight_change(self):
        after=copy.deepcopy(self.p);after['attributes'][1]['value']=self.name
        job.verify(self.p,after,self.name,self.aid)
        after['weight']=2
        with self.assertRaisesRegex(RuntimeError,'Unrelated product'):job.verify(self.p,after,self.name,self.aid)
        after['weight']=1;after['attributes'][0]['value']=26
        with self.assertRaisesRegex(RuntimeError,'custom attribute'):job.verify(self.p,after,self.name,self.aid)


if __name__=='__main__':
    unittest.main()
