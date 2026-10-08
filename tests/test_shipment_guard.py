# поток: fin
import asyncio
import json
import os
import unittest
from datetime import datetime,date,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx
from shipment_guard import config,store,sync
from shipment_guard.economics import assess
from shipment_guard.ms import MSClient,SourceError
from web.shipment_receiver import app,normalize

ACCOUNT='4a8cbaf7-0e7b-11e2-9874-3c4a92f3a0a7'
MID=str(uuid4()); DID=str(uuid4())

def event(did=DID,action='UPDATE'):
    return {'accountId':ACCOUNT,'action':action,'meta':{'type':'demand','href':config.MS+'/entity/demand/'+did}}

def example(qty=1,unit=5048,cost=5975.74):
    doc={'name':'61563303169','updated':'2026-10-08 10:00:00','moment':'2026-10-08 10:00:00','sum':unit*qty*100,
         'organization':{'meta':{'href':config.MS+'/entity/organization/a5e7ee84-8d63-41f2-9fae-0be2ef90462b'}},
         'agent':{'meta':{'href':config.MS+'/entity/counterparty/f125b891-28a5-11ee-0a80-0d3a001d1aa6'}}}
    pos=[{'assortment':{'meta':{'href':config.MS+'/entity/product/'+MID},'externalCode':'7200'},'quantity':qty,'price':unit*100}]
    fifo=[{'positions':[{'meta':{'href':config.MS+'/entity/product/'+MID},'cost':cost*100,'quantity':-999}]}]
    full={'verified':True,'total':unit*qty,'items':{MID:{'total':unit*qty}},'source':'order'}
    rule={'platform':'yandex','retention':.6700368042873839,'valid_until':date(2099,1,1),'version':'test','base_from':date(2026,9,1),'base_to':date(2026,9,30),'source':'verified','source_sha256':'abc'}
    return doc,pos,fifo,full,rule

class EconomicsTests(unittest.TestCase):
    def test_known_loss_and_qty_total_fifo(self):
        r=assess(*example()); self.assertIn('below_fifo',r['signals']); self.assertEqual(r['fifo'],5975.74)
        r=assess(*example(3,2000,4500)); self.assertEqual(r['fifo'],4500); self.assertEqual(r['lines'][0]['quantity'],3)
        self.assertNotIn('below_fifo',r['signals'])
    def test_below_floor_only_and_market_baseline(self):
        d,p,f,c,rule=example(1,1156,702.70); r=assess(d,p,f,c,rule)
        self.assertNotIn('below_fifo',r['signals']); self.assertIn('below_floor',r['signals'])
        self.assertAlmostEqual(r['floor'],(702.7+300)/(1-.6700368042873839),places=2)
    def test_missing_price_and_fifo_visible(self):
        d,p,f,c,rule=example(); d['sum']=None; f=[]
        r=assess(d,p,f,c,rule); self.assertEqual(r['status'],'data_error'); self.assertIsNone(r['fifo'])
    def test_nominal_ms_preserves_full(self):
        d,p,f,c,rule=example(); d['sum']=100;p[0]['price']=100
        r=assess(d,p,f,c,rule);self.assertEqual(r['document_price'],1);self.assertEqual(r['full_price'],5048);self.assertIn('below_fifo',r['signals'])
    def test_repeated_product_and_ambiguous_rows(self):
        d,p,f,c,rule=example(1,3000,3500); p+=[dict(p[0],price=100000)];d['sum']=400000;c['total']=4000;c['items'][MID]['total']=4000
        r=assess(d,p,f,c,rule);self.assertEqual(len(r['lines']),1);self.assertEqual(r['lines'][0]['quantity'],2);self.assertEqual(r['fifo'],3500);self.assertIn('не распределён',r['lines'][0]['level'])
    def test_one_losing_item_not_hidden(self):
        d,p,f,c,rule=example(1,1000,2000);mid2=str(uuid4()); p.append({'assortment':{'meta':{'href':config.MS+'/entity/product/'+mid2}},'quantity':1,'price':1000000}); f[0]['positions'].append({'meta':{'href':config.MS+'/entity/product/'+mid2},'cost':100000}); d['sum']=1100000;c['total']=11000;c['items'][mid2]={'total':10000}
        r=assess(d,p,f,c,rule);self.assertIn('below_fifo',r['signals']);self.assertGreater(r['full_price'],r['fifo'])
    def test_no_commission_not_zero_and_wrong_platform(self):
        d,p,f,c,rule=example();rule['platform']='ozon';r=assess(d,p,f,c,rule);self.assertIn('rule_unknown',r['signals']);self.assertIsNone(r['floor']);self.assertIn('below_fifo',r['signals'])
    def test_returns_negative_qty_visible(self):
        d,p,f,c,rule=example();p[0]['quantity']=-1;r=assess(d,p,f,c,rule);self.assertEqual(r['status'],'data_error')

class ReceiverTests(unittest.TestCase):
    def test_all_events_and_bad_href(self):
        events=normalize({'events':[event(),event(str(uuid4()),'DELETE')]},ACCOUNT);self.assertEqual(len(events),2)
        e=event();e['meta']['href']='https://evil.test/'+DID
        with self.assertRaises(ValueError):normalize({'events':[e]},ACCOUNT)
    def test_rejects_wrong_account(self):
        e=event();e['accountId']=str(uuid4())
        with self.assertRaises(ValueError):normalize({'events':[e]},ACCOUNT)
    def test_ack_only_after_save_and_db_failure(self):
        async def check():
            with patch.dict(os.environ,{'SHIPMENT_GUARD_WEBHOOK_SECRET':'test-secret','SHIPMENT_GUARD_ACCOUNT_ID':ACCOUNT}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
                    url='/hooks/ms-shipment/test-secret?requestId='+str(uuid4())
                    with patch('web.shipment_receiver.store.accept',return_value=True) as save:
                        r=await c.post(url,json={'events':[event(),event(str(uuid4()))]});self.assertEqual(r.status_code,204);self.assertEqual(len(save.call_args.args[3]),2)
                    with patch('web.shipment_receiver.store.accept',side_effect=RuntimeError),patch('web.shipment_receiver.store.metric'):
                        r=await c.post(url,json={'events':[event()]});self.assertEqual(r.status_code,503)
                    self.assertEqual((await c.post('/hooks/ms-shipment/bad',json={})).status_code,404)
        asyncio.run(check())

class LimiterTests(unittest.TestCase):
    def test_100_documents_and_429_shared_pause(self):
        now=[0.]; starts=[]
        class Response:
            status_code=200;headers={}
            def json(self):return {'rows':[]}
        class Session:
            def get(self,*a,**kw):
                starts.append(now[0]);r=Response()
                if len(starts)==2:r.status_code=429;r.headers={'Retry-After':'7'}
                return r
        client=MSClient(Session(),lambda:now[0],lambda n:now.__setitem__(0,now[0]+n))
        with patch('shipment_guard.ms.store.metric'),patch.dict(os.environ,{'MOYSKLAD_TOKEN':'test'}):
            for _ in range(300):
                try:client.get('/entity/demand/'+DID)
                except SourceError:pass
        self.assertTrue(all(b-a>=1 for a,b in zip(starts,starts[1:])));self.assertGreaterEqual(starts[2]-starts[1],7)
    def test_remaining_quota_pauses_all_paths(self):
        now=[0.]
        class Session:
            def get(self,*a,**kw):
                class R:
                    status_code=200;headers={'X-RateLimit-Remaining':'1'}
                    def json(self):return {}
                return R()
        c=MSClient(Session(),lambda:now[0],lambda n:now.__setitem__(0,now[0]+n))
        with patch('shipment_guard.ms.store.metric'),patch.dict(os.environ,{'MOYSKLAD_TOKEN':'test'}):
            c.get('/entity/demand/'+DID);c.get('/report/stock/byoperation')
        self.assertGreaterEqual(now[0],5)

@unittest.skipUnless(os.getenv('SHIPMENT_GUARD_TEST_DB')=='1','isolated PostgreSQL integration tests explicitly enabled')
class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from core import db
        cls.schema='test_shipment_'+uuid4().hex;cls.original=psycopg2.connect
        with cls.original(db.DATABASE_URL) as conn:
            with conn.cursor() as c:c.execute('CREATE SCHEMA '+cls.schema)
        def connect(*args,**kw):
            kw['options']=kw.get('options','')+' -c search_path='+cls.schema
            return cls.original(*args,**kw)
        cls.patcher=patch('psycopg2.connect',side_effect=connect);cls.patcher.start()
        with store.transaction() as c:c.execute((Path(__file__).parents[1]/'migrations/071_shipment_guard.sql').read_text())
    @classmethod
    def tearDownClass(cls):
        from core import db
        cls.patcher.stop()
        with cls.original(db.DATABASE_URL) as conn:
            with conn.cursor() as c:c.execute('DROP SCHEMA '+cls.schema+' CASCADE')
    def setUp(self):
        with store.transaction() as c:c.execute('TRUNCATE ms_webhook_inbox,ms_shipment_jobs,ms_shipment_assessments,ms_shipment_sync_state,ms_shipment_metrics RESTART IDENTITY')
    def push(self,ds=None,rid=None):
        events=[event(d) for d in (ds or [DID])];p={'events':events}
        return store.accept(ACCOUNT,rid or str(uuid4()),p,normalize(p,ACCOUNT))
    def ready(self):
        with store.transaction() as c:c.execute("UPDATE ms_shipment_jobs SET next_attempt_at=now()-interval '1 second'")
    def jobs(self):
        with store.transaction() as c:c.execute('SELECT * FROM ms_shipment_jobs');return [dict(r) for r in c.fetchall()]
    def test_multi_event_duplicate_and_100_batch(self):
        ids=[str(uuid4()) for _ in range(100)];rid=str(uuid4());self.assertTrue(self.push(ids,rid));self.assertFalse(self.push(ids,rid));self.assertEqual(len(self.jobs()),100);self.assertTrue(all(j['requested_generation']==1 for j in self.jobs()))
    def test_crash_claim_lease_and_old_owner(self):
        self.push();self.ready();old=store.claim()
        self.assertIsNone(store.claim())
        with store.transaction() as c:c.execute("UPDATE ms_shipment_jobs SET locked_until=now()-interval '1 second'")
        new=store.claim();self.assertNotEqual(old['lease_token'],new['lease_token']);self.assertFalse(store.finish(old,{'status':'ok'}));self.assertTrue(store.finish(new,{'status':'ok'}))
    def test_update_during_processing_does_not_publish_old(self):
        self.push();self.ready();j=store.claim();self.push();self.assertFalse(store.finish(j,{'status':'below_fifo','signals':['below_fifo']}));self.assertEqual(self.jobs()[0]['state'],'pending');self.assertEqual(self.jobs()[0]['requested_generation'],2)
        with store.transaction() as c:c.execute('SELECT count(*) n FROM ms_shipment_assessments WHERE is_current');self.assertEqual(c.fetchone()['n'],0)
    def test_same_result_no_spam_then_delete_history(self):
        for _ in range(2):self.push();self.ready();store.finish(store.claim(),{'status':'below_fifo','signals':['below_fifo']})
        self.push();self.ready();store.finish(store.claim(),{'status':'deleted','signals':[]})
        with store.transaction() as c:
            c.execute('SELECT count(*) n FROM ms_shipment_assessments');self.assertEqual(c.fetchone()['n'],2)
            c.execute('SELECT status FROM ms_shipment_assessments WHERE is_current');self.assertEqual(c.fetchone()['status'],'deleted')
    def test_failed_page_preserves_checkpoint_and_saved_jobs(self):
        before=datetime.now(timezone.utc)-timedelta(days=2)
        with store.transaction() as c:c.execute('INSERT INTO ms_shipment_sync_state(account_id,checkpoint) VALUES(%s,%s)',(ACCOUNT,before))
        class Client:
            def get(self,*a,**kw):
                if a[1]['offset']:raise RuntimeError('page failed')
                return {'rows':[{'id':DID,'accountId':ACCOUNT,'updated':'2026-10-08 10:00:00'}],'meta':{'size':2}}
        g=sync.reconcile(Client(),ACCOUNT);next(g)
        with self.assertRaises(RuntimeError):next(g)
        self.assertEqual(len(self.jobs()),1)
        with store.transaction() as c:c.execute('SELECT checkpoint FROM ms_shipment_sync_state');self.assertEqual(c.fetchone()['checkpoint'],before)
    def test_sync_missing_create_timezone_and_overlap(self):
        before=datetime(2026,10,7,21,tzinfo=timezone.utc)
        with store.transaction() as c:c.execute('INSERT INTO ms_shipment_sync_state(account_id,checkpoint) VALUES(%s,%s)',(ACCOUNT,before))
        params=[]
        class Client:
            def get(self,path,p):params.append(p);return {'rows':[{'id':DID,'accountId':ACCOUNT,'updated':'2026-10-08 00:00:00'}],'meta':{'size':1}}
        list(sync.reconcile(Client(),ACCOUNT,datetime(2026,10,8,22,tzinfo=timezone.utc)))
        self.assertEqual(self.jobs()[0]['first_source'],'reconciliation');self.assertIn('2026-10-07 23:55:00',params[0]['filter'])
    def test_transaction_rollback_no_ack_state(self):
        with self.assertRaises(Exception):self.push([DID,'not-a-uuid'])
        self.assertEqual(self.jobs(),[])

    @unittest.skipUnless(os.getenv('SHIPMENT_GUARD_TEST_DB')=='1','isolated DB tests')
    def test_unstable_pages_never_advance_checkpoint(self):
        before=datetime.now(timezone.utc)-timedelta(days=2)
        with store.transaction() as c:c.execute('INSERT INTO ms_shipment_sync_state(account_id,checkpoint) VALUES(%s,%s)',(ACCOUNT,before))
        class Client:
            n=0
            def get(self,*a,**kw):
                self.n+=1
                return {'rows':[{'id':str(uuid4()),'accountId':ACCOUNT,'updated':'2026-10-08 10:00:00'}],'meta':{'size':1}}
        with self.assertRaises(RuntimeError):list(sync.reconcile(Client(),ACCOUNT))
        with store.transaction() as c:c.execute('SELECT checkpoint FROM ms_shipment_sync_state');self.assertEqual(c.fetchone()['checkpoint'],before)

class AdapterTests(unittest.TestCase):
    def test_yandex_total_price_not_multiplied_by_count(self):
        from shipment_guard.adapters import OrderAdapter
        a=OrderAdapter()
        with patch('shipment_guard.adapters.db.query',return_value=[{'payload':{'items':[{'shopSku':'7200','count':3,'prices':[{'total':12000},{'total':3000}]}]},'loaded_at':'2026-10-08'}]):
            raw,items,src=a.yandex('ya_acc1','123')
        self.assertEqual(items,[('7200',3,15000)])
    def test_partial_order_uses_verified_unit_price_and_document_qty(self):
        from shipment_guard.adapters import OrderAdapter
        d,p,f,c,r=example(2,2000,3000);a=OrderAdapter()
        with patch.object(a,'yandex',return_value=({},[('7200',3,6000),('other',1,5000)],'order')):
            full,raw=a.confirm(d,p)
        self.assertTrue(full['verified']);self.assertEqual(full['total'],4000)
    def test_partial_sku_different_prices_is_ambiguous(self):
        from shipment_guard.adapters import OrderAdapter
        d,p,f,c,r=example(1,2000,3000);a=OrderAdapter()
        with patch.object(a,'yandex',return_value=({},[('7200',1,2000),('7200',1,3000)],'order')):
            full,raw=a.confirm(d,p)
        self.assertIn('неоднозначна',full['error'])
    def test_ozon_unit_sale_price_times_quantity(self):
        from shipment_guard.adapters import OrderAdapter
        a=OrderAdapter()
        with patch('shipment_guard.adapters.db.query',return_value=[{'payload':{'products':[{'offer_id':'7200','quantity':3,'price':'2000.50','currency_code':'RUB'}]},'loaded_at':'now'}]):
            raw,items,src=a.ozon('oz_acc1','123-001')
        self.assertEqual(items,[('7200',3,6001.5)])
    def test_worker_reads_current_state_for_out_of_order_delete(self):
        from shipment_guard.worker import Worker
        d,p,f,c,rule=example();d['accountId']=ACCOUNT
        class Client:
            requests=0;pulse=None
            def get(self,path):return d
            def rows(self,path,params):return iter(p if '/positions' in path else f)
        from unittest.mock import Mock
        adapter=Mock();adapter.confirm.return_value=(c,{})
        w=Worker(Client(),adapter);job={'account_id':ACCOUNT,'demand_id':DID,'requested_at':datetime.now(timezone.utc),'requested_generation':1}
        with patch('shipment_guard.worker.db.query',return_value=[rule]),patch('shipment_guard.worker.store.finish') as finish,patch('shipment_guard.worker.store.metric'):
            w.process(job)
        self.assertEqual(finish.call_args.args[1]['status'],'below_fifo')

class MoreWorkerTests(unittest.TestCase):
    def test_deleted_document_closes_actual_signal(self):
        from shipment_guard.worker import Worker
        from shipment_guard.ms import NotFound
        from unittest.mock import Mock
        client=Mock();client.requests=0;client.get.side_effect=NotFound('ms_404')
        w=Worker(client,Mock());job={'account_id':ACCOUNT,'demand_id':DID,'requested_at':datetime.now(timezone.utc)}
        with patch('shipment_guard.worker.db.query',return_value=[{'result':{'status':'below_fifo','signals':['below_fifo'],'name':'order'}}]),patch('shipment_guard.worker.store.finish') as finish,patch('shipment_guard.worker.store.metric'):
            w.process(job)
        result=finish.call_args.args[1];self.assertEqual(result['status'],'deleted');self.assertEqual(result['signals'],[])
    def test_403_is_error_not_deleted(self):
        from shipment_guard.worker import Worker
        from unittest.mock import Mock
        client=Mock();client.requests=0;client.get.side_effect=SourceError('ms_http_403')
        w=Worker(client,Mock());job={'account_id':ACCOUNT,'demand_id':DID,'requested_at':datetime.now(timezone.utc)}
        with patch('shipment_guard.worker.store.finish') as finish,patch('shipment_guard.worker.store.metric'):
            w.process(job)
        self.assertEqual(finish.call_args.args[1]['status'],'data_error')


class WBCredentialsTests(unittest.TestCase):
    def test_selected_existing_token_and_verified_rub_conversion(self):
        from shipment_guard.adapters import OrderAdapter
        from unittest.mock import Mock
        a=OrderAdapter();a.request=Mock(return_value={'orders':[{'id':123,'article':'7200','currencyCode':398,'convertedCurrencyCode':643,'price':99999,'convertedPrice':504800}],'next':0})
        with patch('shipment_guard.adapters.db.query',return_value=[]),patch.dict(os.environ,{'SHIPMENT_GUARD_WB_TOKEN_ENV_ACC1':'WB_TOKEN_RETURNS_ACC1','WB_TOKEN_RETURNS_ACC1':'test-token'}):
            raw,items,source=a.wb('wb_acc1','123',{'moment':'2026-10-08 10:00:00'})
        self.assertEqual(items,[('7200',1,5048)]);self.assertIn('convertedPrice',source)
        self.assertEqual(a.request.call_args.kwargs['headers']['Authorization'],'test-token')
    def test_reject_cross_account_token_reference(self):
        from shipment_guard.adapters import OrderAdapter
        a=OrderAdapter()
        with patch('shipment_guard.adapters.db.query',return_value=[]),patch.dict(os.environ,{'SHIPMENT_GUARD_WB_TOKEN_ENV_ACC1':'WB_TOKEN_RETURNS_ACC2'}):
            with self.assertRaises(SourceError):a.wb('wb_acc1','123',{'moment':'2026-10-08 10:00:00'})
