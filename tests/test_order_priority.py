# поток: inv
from datetime import datetime,timezone,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,unittest
from ops.order_priority import order_key,is_express
from ops.fbs_ms_order_payload import EXPRESS_AGENT
from ops import fbs_ms_post_import_status as status, fbs_ms_post_import_shipments as shipment
from ops import ms_supply_fulfillment as supply

NORMAL='b7e22828-0b7c-11ee-0a80-11480012ed47'
def order(name,stamp,express=False):
    agent=EXPRESS_AGENT['ozon'] if express else NORMAL
    return {'id':name,'name':name,'moment':stamp,'agent':{'meta':{'href':'https://ms/entity/counterparty/'+agent}}}

class PriorityTests(unittest.TestCase):
    def test_express_first_then_oldest_even_across_timezones(self):
        values=[order('normal-new','2026-11-05 11:00:00'),order('express-new','2026-11-05T09:00:00Z',True),
                order('normal-old','2026-11-04 10:00:00'),order('express-old','2026-11-05 10:00:00',True)]
        self.assertEqual([v['name'] for v in sorted(values,key=order_key)],
                         ['express-old','express-new','normal-old','normal-new'])

    def test_both_ms_express_agents_and_explicit_mp_flags(self):
        for uid in EXPRESS_AGENT.values():self.assertTrue(is_express({'agent_id':uid}))
        self.assertTrue(is_express({'market':{'is_express':True}}))
        self.assertTrue(is_express({'mp_payload':{'market_order':{'programType':'EXPRESS'}}}))
        self.assertFalse(is_express({'name':'экспресс','is_express':'true'}))

    def test_original_order_time_wins_over_queue_time(self):
        values=[{'name':'older','moment':'2026-11-01 10:00:00','created':'2026-11-05 12:00:00'},
                {'name':'newer','moment':'2026-11-04 10:00:00','created':'2026-11-04 11:00:00'}]
        self.assertEqual(sorted(values,key=order_key)[0]['name'],'older')
        self.assertLess(order_key({'name':'known','moment':'2026-11-01 10:00:00'}),order_key({'name':'unknown'}))

    def test_status_express_before_write_limit(self):
        normal=order('normal','2026-11-01 10:00:00');express=order('express','2026-11-05 11:00:00',True)
        for o in [normal,express]:o['state']={'name':'Ожидает подтверждения'}
        with TemporaryDirectory() as tmp,patch.object(status,'RUNTIME',Path(tmp)), \
             patch.object(status,'LOCK',Path(tmp)/'lock'),patch.object(status,'HANDLED',Path(tmp)/'handled'), \
             patch.object(status,'MAX_WRITES',1), \
             patch.object(status,'imported_proofs',return_value={'normal':{},'express':{}}), \
             patch.object(status,'current_orders',return_value={'normal':normal,'express':express}), \
             patch.object(status,'apply_one',side_effect=lambda o,*_:{'number':o['name'],'result':'updated'}) as apply:
            result=status.run(True)
        self.assertEqual(apply.call_args.args[0]['name'],'express')
        self.assertEqual(result['updated'],1);self.assertEqual(result['deferred'],1)

    def test_existing_shipment_journals_use_agent_and_order_time(self):
        with TemporaryDirectory() as tmp,patch.object(shipment,'STATUS_RUNTIME',Path(tmp)/'status'), \
             patch.object(shipment,'RUNTIME',Path(tmp)/'status/shipment'), \
             patch.object(shipment.ms_api,'get',side_effect=AssertionError('no priority API requests')):
            values=[order('normal','2026-11-01 10:00:00'),order('express-new','2026-11-05 10:00:00',True),
                    order('express-old','2026-11-04 10:00:00',True)]
            for n,o in enumerate(values):
                folder=shipment.STATUS_RUNTIME/o['name'];folder.mkdir(parents=True)
                proof={'name':o['name'],'state':'Собирается','shipment_due':True,
                       'changed_at':(datetime(2026,11,5,tzinfo=timezone.utc)+timedelta(minutes=n)).isoformat()}
                (folder/'readback.json').write_text(json.dumps(proof))
                (folder/'response.json').write_text(json.dumps({'body':o}))
            self.assertEqual([p['name'] for p in shipment.pending()],['express-old','express-new','normal'])

    def test_receipt_resume_prioritizes_express_before_cap(self):
        values={'normal':order('normal','2026-11-01 10:00:00'),
                'express':order('express','2026-11-05 10:00:00',True)}
        job={'order_ids':['normal','express'],'candidates':values,'results':{},'receipt_id':'r','version':'v','budget':{}}
        with TemporaryDirectory() as tmp,patch.object(supply,'MAX_ORDERS',1), \
             patch.object(supply,'checkpoint_path',side_effect=lambda cid:Path(tmp)/cid/'checkpoint'), \
             patch.object(supply,'load',return_value=None),patch.object(supply,'ledger_load',return_value={'active':{}}), \
             patch.object(supply,'same_order',return_value=True),patch.object(supply,'allowed_fbs',return_value=True), \
             patch.object(supply,'intersects',return_value=True),patch.object(supply,'availability',return_value=(True,{})), \
             patch.object(supply,'claim',side_effect=lambda o,*_:{'original':o}) as claim, \
             patch.object(supply,'change_state',return_value={'stage':'ms_changed'}),patch.object(supply,'save'):
            supply.status_stage(job,Path(tmp))
        self.assertEqual(claim.call_args.args[0]['name'],'express')
        self.assertIn('express',job['results']);self.assertNotIn('normal',job['results'])

    def test_receipt_due_shipments_sorted_before_cap(self):
        values={'normal':order('normal','2026-11-01 10:00:00'),
                'express':order('express','2026-11-05 10:00:00',True)}
        job={'order_ids':['normal','express'],'candidates':values,'status':'active',
             'results':{'normal':{'result':'ms_changed'},'express':{'result':'ms_changed'}}}
        with TemporaryDirectory() as tmp,patch.object(supply,'RUNTIME',Path(tmp)), \
             patch.object(supply,'new_job',return_value=(job,Path(tmp))),patch.object(supply,'MAX_ORDERS',1), \
             patch.object(supply,'load',return_value={'shipment_due_at':'2026-11-01T00:00:00+00:00'}), \
             patch.object(supply,'shipment_stage',return_value=1) as stage,patch.object(supply,'result_for',return_value={}):
            supply.fulfill_receipt('00000000-0000-0000-0000-000000000001','event',apply=True)
        self.assertEqual(stage.call_args.args[2],['express'])

    def test_importer_express_before_creation_limit(self):
        from ops import fbs_ms_import_scheduler as scheduler
        rows=[{'number':name,'platform':'ozon','account':'oz_acc2','status':'awaiting_packaging',
               'created_at':stamp,'is_express':express}
              for name,stamp,express in [('normal','2026-11-01T00:00:00Z',False),
                                         ('express','2026-11-05T00:00:00Z',True)]]
        with TemporaryDirectory() as tmp,patch.object(scheduler,'OUT',Path(tmp)), \
             patch.object(scheduler,'RUNTIME',Path(tmp)/'schedule'), \
             patch.object(scheduler,'LOCK',Path(tmp)/'schedule/lock'), \
             patch.object(scheduler,'KNOWN',Path(tmp)/'schedule/known.json'), \
             patch.object(scheduler,'STATE',Path(tmp)/'schedule/state.json'), \
             patch.object(scheduler,'source_rows',return_value=(rows,'start','end')), \
             patch.object(scheduler,'known_ms_numbers',return_value=set()), \
             patch.object(scheduler,'exact_ms_numbers',return_value=set()), \
             patch.object(scheduler,'prices_for',return_value=({},'digest')), \
             patch.object(scheduler,'enter_digest',return_value='digest'), \
             patch.object(scheduler,'find_customerorder',return_value=None), \
             patch.object(scheduler,'MAX_PER_RUN',1), \
             patch('ops.fbs_ms_import_next5.create_one',return_value={'number':'express','result':'created'}) as create:
            result=scheduler.run(True)
        self.assertEqual(create.call_args.args[0]['number'],'express')
        self.assertFalse(result['cursor_advanced'])

    def test_marketplace_mutations_keep_express_first(self):
        from ops.fbs_market_fulfillment import FulfillmentSession
        values=[order('normal','2026-11-01 10:00:00'),order('express','2026-11-05 10:00:00',True)]
        for value in values:
            value['organization']={'meta':{'href':'https://ms/entity/organization/a5e7ee84-8d63-41f2-9fae-0be2ef90462b'}}
        session=FulfillmentSession()
        with TemporaryDirectory() as tmp,patch.object(session,'fulfill',return_value={'result':'ready'}) as fulfill:
            session.fulfill_many(values,Path(tmp))
        self.assertEqual([call.args[0]['name'] for call in fulfill.call_args_list],['express','normal'])
