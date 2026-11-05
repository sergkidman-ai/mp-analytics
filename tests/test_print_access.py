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
        batches = {
            self.digital: dict(counter_key='oz_acc1|digital-secret|store', orders=[{'account':'oz_acc1'}]),
            self.diskver: dict(counter_key='oz_acc2|diskver|store', orders=[{'account':'oz_acc2'}]),
            self.mixed: dict(counter_key='oz_acc2|mixed|store', orders=[{'account':'oz_acc2'},{'account':'oz_acc1'}]),
        }
        for bid, content in ((self.digital,b'digital-secret'),(self.diskver,b'diskver-document')):
            (self.root/(bid+'.pdf')).write_bytes(content)
        root,calls=self.root,self.calls

        class Queue:
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
