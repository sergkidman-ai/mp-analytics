# поток: inv
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch,Mock
import fcntl,json,os,time,unittest
from ops import customer_guard_ms_recovery as recovery

ROW={'id':1,'platform':'ozon','account':'oz_acc2','posting_number':'123-0001-1',
     'mp_payload':{'products':[{'offer_id':'0001','quantity':1}]}}
SOURCE={'platform':'ozon','account':'acc2','number':ROW['posting_number'],
        'products':[{'code':'0001','quantity':1}]}

def processed(numbers):
    return {'status':{'results':[{'number':n,'result':'updated'} for n in numbers],'errors':[]},
            'shipments':{'results':[],'errors':[]}}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.stack=ExitStack();self.addCleanup(self.stack.close)
        def set_(obj,key,value):return self.stack.enter_context(patch.object(obj,key,value))
        set_(recovery,'RUNTIME',self.root/'recovery')
        set_(recovery.events,'EVENT_ROOT',self.root/'events')
        recovery.events.publish({'order_id':ROW['id'],'check_id':100,'platform':ROW['platform'],
                                 'account':ROW['account'],'number':ROW['posting_number']})
        set_(recovery.scheduler,'LOCK',self.root/'shared.lock')
        set_(recovery.scheduler,'KNOWN',self.root/'known.json')
        set_(recovery.importer,'OUT',self.root/'imports')
        self.candidates=set_(recovery,'candidates',Mock(return_value=[ROW]))
        self.find=set_(recovery,'find_customerorder',Mock(return_value=None))
        self.fresh=set_(recovery,'fresh_source',Mock(return_value=SOURCE))
        self.price=set_(recovery.scheduler,'prices_for',Mock(return_value=({},'digest')))
        set_(recovery.scheduler,'known_ms_numbers',Mock(return_value=set()))
        def create(row,*args):
            folder=recovery.importer.OUT/('next5_'+row['number']);folder.mkdir(parents=True)
            (folder/'preflight.json').write_text(json.dumps({'selected':[],'payload':{}}))
            (folder/'readback.json').write_text(json.dumps({'id':'ms-id','name':row['number']}))
            return {'number':row['number'],'result':'created'}
        self.create=set_(recovery.importer,'create_one',Mock(side_effect=create))
        self.process=set_(recovery,'process',Mock(side_effect=processed))

    def ledger(self):return json.loads((recovery.RUNTIME/'ledger.json').read_text())

    def test_preview_never_calls_ms_or_importer(self):
        result=recovery.run(False)
        self.assertEqual(result['candidates'],1)
        self.find.assert_not_called();self.create.assert_not_called();self.process.assert_not_called()

    def test_no_event_means_no_database_or_external_calls(self):
        for path in recovery.events.event_files():path.unlink()
        recovery.run(True)
        self.candidates.assert_not_called();self.find.assert_not_called();self.create.assert_not_called()
        self.assertFalse((recovery.events.EVENT_ROOT/'pending').exists())

    def test_duplicate_event_does_not_reopen_completed_order(self):
        recovery.run(True)
        recovery.events.publish({'order_id':1,'check_id':100,'platform':'ozon',
                                 'account':'oz_acc2','number':ROW['posting_number']})
        recovery.run(True)
        self.assertEqual(self.create.call_count,1);self.assertEqual(self.candidates.call_count,1)

    def test_stale_event_revalidated_without_any_ms_calls(self):
        self.candidates.return_value=[]
        result=recovery.run(True)
        self.find.assert_not_called();self.create.assert_not_called()
        self.assertFalse(result['retry_pending'])

    def test_event_not_acknowledged_before_durable_receipt(self):
        original=recovery.scheduler.atomic_save
        with patch.object(recovery.scheduler,'atomic_save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):recovery.run(True)
        self.assertTrue(recovery.events.event_files());self.find.assert_not_called()
        recovery.run(True)
        self.assertEqual(self.create.call_count,1)

    def test_external_ms_order_not_created_or_processed(self):
        self.find.return_value={'id':'external'}
        result=recovery.run(True)
        self.assertEqual(result['existing'],[ROW['posting_number']])
        self.create.assert_not_called();self.fresh.assert_not_called();self.process.assert_not_called()

    def test_import_uses_standard_importer_and_processes_then_cools_down(self):
        result=recovery.run(True)
        self.assertEqual(len(result['created']),1);self.create.assert_called_once()
        self.process.assert_called_once()
        self.assertTrue(self.ledger()[recovery.key(ROW)]['processed'])
        recovery.run(True)
        self.assertEqual(self.create.call_count,1);self.assertEqual(self.find.call_count,1)

    def test_old_guard_data_cannot_import_cancelled_order(self):
        self.fresh.side_effect=RuntimeError('ozon_status_changed')
        result=recovery.run(True)
        self.assertTrue(result['errors']);self.create.assert_not_called();self.price.assert_not_called()
        recovery.run(True);self.assertEqual(self.fresh.call_count,1)

    def test_shared_import_lock_skips_without_any_ms_calls(self):
        fd=os.open(recovery.scheduler.LOCK,os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertTrue(recovery.run(True)['busy'])
        finally:os.close(fd)
        self.find.assert_not_called();self.create.assert_not_called()

    def test_processing_interruption_resumes_without_reposting(self):
        self.process.side_effect=RuntimeError('status_step_busy')
        recovery.run(True)
        ledger=self.ledger();record=ledger[recovery.key(ROW)];self.assertTrue(record['needs_processing'])
        record['processing_next_at']=0
        (recovery.RUNTIME/'ledger.json').write_text(json.dumps(ledger))
        self.process.side_effect=processed
        recovery.run(True)
        self.assertEqual(self.create.call_count,1)
        self.assertTrue(self.ledger()[recovery.key(ROW)]['processed'])

    def test_three_creations_max_per_pass(self):
        for path in recovery.events.event_files():path.unlink()
        self.candidates.return_value=[dict(ROW,id=i,posting_number=f'{i}-0001-1') for i in range(1,7)]
        for row in self.candidates.return_value:
            recovery.events.publish({'order_id':row['id'],'check_id':100+row['id'],'platform':row['platform'],
                                     'account':row['account'],'number':row['posting_number']})
        self.fresh.side_effect=lambda row,*_:dict(SOURCE,number=row['posting_number'])
        result=recovery.run(True)
        self.assertEqual(len(result['created']),3);self.assertEqual(self.create.call_count,3)

    def test_fresh_express_event_precedes_creation_cap(self):
        for path in recovery.events.event_files():path.unlink()
        rows=[dict(ROW,id=i,posting_number=f'{i}-0001-1') for i in range(1,7)]
        self.candidates.return_value=rows
        for row in rows:
            recovery.events.publish({'order_id':row['id'],'check_id':100+row['id'],
                'platform':row['platform'],'account':row['account'],'number':row['posting_number']})
        self.fresh.side_effect=lambda row,*_:dict(SOURCE,number=row['posting_number'],
            created_at=f"2026-11-05T0{row['id']}:00:00Z",is_express=row['id']==6)
        result=recovery.run(True)
        self.assertEqual([r['number'] for r in result['created']],['6-0001-1','1-0001-1','2-0001-1'])

    def test_existing_our_document_is_processed_without_recreation(self):
        self.create.side_effect(SOURCE)
        self.find.return_value={'id':'ms-id'}
        result=recovery.run(True)
        self.assertEqual(result['existing'],[ROW['posting_number']]);self.create.assert_not_called()
        self.process.assert_called_once()

    def test_existing_after_uncertain_post_requires_reconciliation_without_new_post(self):
        folder=recovery.importer.OUT/('next5_'+ROW['posting_number']);folder.mkdir(parents=True)
        (folder/'intent.json').write_text('{}')
        self.find.return_value={'id':'external-or-uncertain'}
        result=recovery.run(True)
        self.assertEqual(result['errors'][0]['reason'],'existing_import_requires_reconciliation')
        self.create.assert_not_called();self.process.assert_not_called()


class FreshSourceTests(unittest.TestCase):
    def test_ozon_uses_point_source_without_any_account_feed_scan(self):
        market={'created_at':'2026-11-05T09:00:00Z','status':'awaiting_packaging',
                'lines':[{'market_code':'0001','quantity':2}]}
        with patch.object(recovery.importer,'source',return_value=market) as source:
            row=recovery.fresh_source(ROW,{}, {},{})
        self.assertEqual(row['products'],[{'code':'0001','quantity':2}])
        self.assertEqual(source.call_args.args[0]['number'],ROW['posting_number'])

    def test_wb_fresh_source_preserves_conversion_and_reuses_daily_statistics(self):
        raw={'id':123,'deliveryType':'fbs','createdAt':'2026-11-05T04:00:00.6Z',
             'rid':'rid','article':'0001','price':999,'currencyCode':933,
             'convertedPrice':30000,'convertedCurrencyCode':643}
        row=dict(ROW,platform='wb',account='wb_acc2',posting_number='123')
        market={'created_at':raw['createdAt'],'status':['new','waiting'],
                'lines':[{'market_code':'0001','quantity':1}]}
        new,stats,days={},{},{}
        with patch.object(recovery,'WBClient') as client, \
             patch.object(recovery.importer,'wb_statistics',return_value={'rid':[]}) as statistics, \
             patch.object(recovery.importer,'source',return_value=market) as source:
            client.return_value.call.return_value={'orders':[raw]}
            first=recovery.fresh_source(row,new,stats,days)
            recovery.fresh_source(row,new,stats,days)
            statistics.assert_called_once_with('acc2');client.return_value.call.assert_called_once()
            self.assertEqual(source.call_args.args[0]['converted_price_kopeks'],30000)
            self.assertEqual(source.call_args.args[0]['currency_code'],933)
            self.assertEqual(first['products'],[{'code':'0001','quantity':1}])

    def test_wb_statistics_failure_is_not_repeated_for_every_order(self):
        raw={'id':123,'deliveryType':'fbs','createdAt':'2026-11-05T04:00:00Z',
             'rid':'rid','article':'0001','price':100,'currencyCode':643}
        row=dict(ROW,platform='wb',account='wb_acc2',posting_number='123')
        new,stats,days={},{},{}
        with patch.object(recovery,'WBClient') as client, \
             patch.object(recovery.importer,'wb_statistics',side_effect=RuntimeError('statistics_failed')) as statistics:
            client.return_value.call.return_value={'orders':[raw]}
            with self.assertRaises(RuntimeError):recovery.fresh_source(row,new,stats,days)
            with self.assertRaisesRegex(RuntimeError,'temporarily_unavailable'):
                recovery.fresh_source(row,new,stats,days)
            statistics.assert_called_once()

    def test_processor_scope_excludes_unrelated_imports_and_shipments(self):
        original=recovery.status_step.imported_proofs
        with patch.object(recovery.status_step,'run') as status,patch.object(recovery.shipment_step,'run') as shipment, \
             patch.object(recovery.shipment_step,'pending',return_value=[{'name':'ours'},{'name':'other'}]):
            status.side_effect=lambda _: {'results':list(recovery.status_step.imported_proofs()),'errors':[]}
            shipment.side_effect=lambda _: {'results':recovery.shipment_step.pending(),'errors':[]}
            result=recovery.process({'ours':{'selected':[]}})
            self.assertEqual(result['status']['results'],['ours'])
            self.assertEqual(result['shipments']['results'],[{'name':'ours'}])
        self.assertIs(recovery.status_step.imported_proofs,original)
