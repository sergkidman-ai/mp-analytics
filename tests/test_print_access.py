# поток: print
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from print_service.domain import PrintBlocked
from web.print_app import create_app


class PrintAccessTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.digital, self.diskver, self.mixed = (str(uuid4()) for _ in range(3))
        self.calls = []
        self.digital_order,self.diskver_order,self.mixed_order=(str(uuid4()) for _ in range(3))
        batches = {
            self.digital: dict(counter_key='oz_acc1|digital-secret|store', orders=[{'account':'oz_acc1'}]),
            self.diskver: dict(counter_key='oz_acc2|diskver|store', orders=[{'account':'oz_acc2'}]),
            self.mixed: dict(counter_key='oz_acc2|mixed|store', orders=[{'account':'oz_acc2'},{'account':'oz_acc1'}]),
        }
        for bid, content in ((self.digital,b'digital-secret'),(self.diskver,b'diskver-document')):
            (self.root/(bid+'.pdf')).write_bytes(content)
        root,calls=self.root,self.calls
        order_map={self.digital_order:dict(account='oz_acc1',batch_id=self.digital,batch_state='ready'),
                   self.diskver_order:dict(account='oz_acc2',batch_id=self.diskver,batch_state='ready'),
                   self.mixed_order:dict(account='oz_acc2',batch_id=self.mixed,batch_state='ready')}

        class Queue:
            def order(self, oid):
                if oid not in order_map:raise PrintBlocked('print_order_missing')
                return order_map[oid]

            def batch(self, bid):
                if bid not in batches: raise PrintBlocked('print_batch_missing')
                return batches[bid]

            def ready_files(self, bid):
                calls.append(('ready_files',bid))
                return dict(receipts=root/(bid+'.pdf'),labels=root/(bid+'.pdf'))

        class Service:
            queue=Queue()

            def status(self):
                return deepcopy(self.data)

            def request_counter(self,key,request):
                calls.append(('counter',key))
                return dict(id='queued',state='building',orders=[{'account':'oz_acc2'}])

            def request_sync(self):
                calls.append(('sync',))
                return 'sync-id'

            def request_order(self,oid,key):
                calls.append(('order',oid));return dict(id='cached',state='ready',count=1)

            def order_files(self,oid):
                calls.append(('order_files',oid))
                return self.queue.ready_files(order_map[oid]['batch_id'])

        self.service=Service()
        self.service.data=dict(day='2026-11-05',demo=False,installed=True,worker_healthy=True,
            interval_minutes=30,last_sync='2026-11-05T06:00:00Z',snapshot_day='2026-11-05',
            rows=[dict(account='oz_acc1',external_id='digital-secret'),dict(account='oz_acc2',external_id='diskver-order')],
            counters={'oz_acc1|digital-secret|store':{'count':9},'oz_acc2|diskver|store':{'count':2}},
            ready_batches=[dict(id=self.digital,**{k:v for k,v in batches[self.digital].items() if k!='orders'}),
                           dict(id=self.diskver,**{k:v for k,v in batches[self.diskver].items() if k!='orders'})],
            jobs=[dict(counter_key='wb_acc1|digital-secret|store'),dict(counter_key='wb_acc2|diskver|store')],
            sync=dict(id='sync-id',state='running',progress={'stage':'postings','loaded':50,'processed':10,'total':100,'updated_at':'2026-11-05T06:00:00Z'}),
            future_unscoped_field='digital-secret')
        self.client=TestClient(create_app(lambda:self.service),headers={'X-Pult-User':'tolkachev'})
        self.addCleanup(self.client.close)

    def test_diskver_status_contains_only_its_orders_counters_files_and_jobs(self):
        response=self.client.get('/api/print');self.assertEqual(response.status_code,200)
        data=response.json()
        self.assertEqual([r['account'] for r in data['rows']],['oz_acc2'])
        self.assertEqual(list(data['counters']),['oz_acc2|diskver|store'])
        self.assertEqual([b['id'] for b in data['ready_batches']],[self.diskver])
        self.assertEqual([j['counter_key'] for j in data['jobs']],['wb_acc2|diskver|store'])
        self.assertNotIn('digital-secret',json.dumps(data))
        self.assertNotIn('future_unscoped_field',data)
        self.assertEqual(set(data['sync']['progress']),{'stage','updated_at'})
        self.assertEqual(len(self.service.data['rows']),2)

    def test_single_organization_page_has_no_digital_configuration(self):
        page=self.client.get('/warehouse/print');self.assertEqual(page.status_code,200)
        self.assertIn('<body class="single-organization">',page.text)
        self.assertIn('Печать заказов · Дисквер',page.text)
        self.assertNotIn('Цифровой',page.text)
        self.assertNotIn('oz_acc1',page.text)

    def test_digital_counter_is_rejected_before_queue_access(self):
        for account in ('oz_acc1','wb_acc1','ya_acc1'):
            response=self.client.post('/api/print/counter',json={'counter_key':account+'|warehouse|store','request_key':str(uuid4())})
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.calls,[])
        response=self.client.post('/api/print/counter',json={'counter_key':'oz_acc2|diskver|store','request_key':str(uuid4())})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.calls,[('counter','oz_acc2|diskver|store')])

    def test_foreign_and_mixed_files_are_denied_before_opening_documents(self):
        for bid in (self.digital,self.mixed,str(uuid4())):
            for kind in ('receipts','labels'):
                response=self.client.get(f'/api/print/batches/{bid}/files/{kind}')
                self.assertEqual(response.status_code,404)
        self.assertEqual(self.calls,[])
        for kind in ('receipts','labels'):
            response=self.client.get(f'/api/print/batches/{self.diskver}/files/{kind}')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,b'diskver-document')

    def test_bersenev_sees_only_digital_orders_counters_jobs_and_single_column_page(self):
        headers={'X-Pult-User':'bersenev'}
        data=self.client.get('/api/print',headers=headers).json()
        self.assertEqual([r['account'] for r in data['rows']],['oz_acc1'])
        self.assertEqual(list(data['counters']),['oz_acc1|digital-secret|store'])
        self.assertEqual([b['id'] for b in data['ready_batches']],[self.digital])
        self.assertEqual([j['counter_key'] for j in data['jobs']],['wb_acc1|digital-secret|store'])
        self.assertNotIn('diskver',json.dumps(data))
        self.assertNotIn('future_unscoped_field',data)
        self.assertEqual(set(data['sync']['progress']),{'stage','updated_at'})
        for path in ('/warehouse/print','/warehouse/print/archive'):
            page=self.client.get(path,headers=headers)
            self.assertEqual(page.status_code,200)
            self.assertIn('<body class="single-organization">',page.text)
            self.assertIn('Печать заказов · Цифровой квадрат',page.text)
            self.assertNotIn('Дисквер',page.text)
            self.assertNotIn('oz_acc2',page.text)
        from print_service.access import access_for_user
        for dashboard in (False,True):
            access=access_for_user('BERSENEV',dashboard=dashboard)
            self.assertEqual(access.accounts,frozenset({'oz_acc1','wb_acc1','ya_acc1'}))

    def test_bersenev_cannot_prepare_or_open_diskver_or_mixed_documents(self):
        from unittest.mock import patch
        headers={'X-Pult-User':'bersenev'}
        for account in ('oz_acc2','wb_acc2'):
            response=self.client.post('/api/print/counter',headers=headers,
                json={'counter_key':account+'|warehouse|store','request_key':str(uuid4())})
            self.assertEqual(response.status_code,403)
        for kind in ('receipts','labels'):
            self.assertEqual(self.client.get(f'/api/print/batches/{self.diskver}/files/{kind}',headers=headers).status_code,404)
            self.assertEqual(self.client.get(f'/api/print/orders/{self.diskver_order}/files/{kind}',headers=headers).status_code,404)
        self.assertEqual(self.client.post(f'/api/print/orders/{self.diskver_order}/pair',headers=headers,
            json={'request_key':str(uuid4())}).status_code,404)
        # An allowed counter must not grant access to foreign batch members.
        mixed=dict(counter_key='oz_acc1|mixed|store',orders=[{'account':'oz_acc1'},{'account':'oz_acc2'}])
        with patch.object(self.service.queue,'batch',return_value=mixed):
            self.assertEqual(self.client.get(f'/api/print/batches/{self.mixed}/files/receipts',headers=headers).status_code,404)
            with patch.object(self.service.queue,'order',return_value=dict(account='oz_acc1',batch_id=self.mixed,batch_state='ready')):
                self.assertEqual(self.client.get(f'/api/print/orders/{self.mixed_order}/files/labels',headers=headers).status_code,404)
        self.assertEqual(self.calls,[])
        for kind in ('receipts','labels'):
            response=self.client.get(f'/api/print/batches/{self.digital}/files/{kind}',headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,b'digital-secret')
        self.assertEqual(self.client.post(f'/api/print/orders/{self.digital_order}/pair',headers=headers,
            json={'request_key':str(uuid4())}).status_code,200)
        for account in ('oz_acc1','wb_acc1','ya_acc1'):
            self.assertEqual(self.client.post('/api/print/counter',headers=headers,
                json={'counter_key':account+'|warehouse|store','request_key':str(uuid4())}).status_code,200)

    def test_missing_or_unknown_proxy_identity_is_denied(self):
        for username in ('','unknown'):
            for path in ('/warehouse/print','/api/print'):
                self.assertEqual(self.client.get(path,headers={'X-Pult-User':username}).status_code,403)
            self.assertEqual(self.client.post('/api/print/sync',headers={'X-Pult-User':username}).status_code,403)
        self.assertEqual(self.calls,[])

    def test_owner_keeps_both_organizations_and_full_progress(self):
        headers={'X-Pult-User':'print'}
        data=self.client.get('/api/print',headers=headers).json()
        self.assertEqual(len(data['rows']),2)
        self.assertEqual(data['sync']['progress']['total'],100)
        page=self.client.get('/warehouse/print',headers=headers)
        self.assertIn('Цифровой квадрат',page.text)
        self.assertNotIn('<body class="single-organization">',page.text)
        response=self.client.get(f'/api/print/batches/{self.digital}/files/receipts',headers=headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.content,b'digital-secret')

    def test_diskver_user_can_request_synchronization(self):
        self.assertEqual(self.client.post('/api/print/sync').status_code,200)
        self.assertEqual(self.calls,[('sync',)])

    def test_individual_order_routes_enforce_organization_and_all_batch_members(self):
        for oid in (self.digital_order,self.mixed_order,str(uuid4())):
            self.assertEqual(self.client.post(f'/api/print/orders/{oid}/pair',json={'request_key':str(uuid4())}).status_code,404)
            self.assertEqual(self.client.get(f'/api/print/orders/{oid}/files/receipts').status_code,404)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.client.post(f'/api/print/orders/{self.diskver_order}/pair',json={'request_key':str(uuid4())}).status_code,200)
        for kind in ('receipts','labels'):
            response=self.client.get(f'/api/print/orders/{self.diskver_order}/files/{kind}')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,b'diskver-document')

    def test_error_order_numbers_cannot_expose_another_organization(self):
        from print_service.access import access_for_user
        data={'jobs':[dict(id='mixed',counter_key='oz_acc2|x|y',state='failed',orders=[
            dict(account='oz_acc1',external_id='digital-secret'),dict(account='oz_acc2',external_id='diskver-order')])],
            'rows':[],'ready_batches':[],'counters':{}}
        scoped=access_for_user('tolkachev').status(data)
        self.assertEqual([o['external_id'] for o in scoped['jobs'][0]['orders']],['diskver-order'])
        data['ready_batches']=[dict(counter_key='oz_acc2|x|y',display={'excluded_orders':data['jobs'][0]['orders']})]
        scoped=access_for_user('tolkachev').status(data)
        self.assertEqual([o['external_id'] for o in scoped['ready_batches'][0]['display']['excluded_orders']],['diskver-order'])
        self.assertEqual(len(data['ready_batches'][0]['display']['excluded_orders']),2)
        data['jobs'][0]['orders']=data['jobs'][0]['orders'][:1]
        self.assertEqual(access_for_user('tolkachev').status(data)['jobs'],[])
