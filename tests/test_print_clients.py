# поток: print
import base64
from datetime import date
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import requests

from PIL import Image
from pypdf import PdfReader, PdfWriter

from print_service.clients import MS, TEMPLATE_ID, TEMPLATE_NAME, PrintClients
from print_service.domain import PrintBlocked
from shipment_guard.config import ORGS, AGENTS


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages): writer.add_blank_page(width=164.25, height=113.25)
    buffer = BytesIO(); writer.write(buffer)
    return buffer.getvalue()


class Response:
    def __init__(self, data=None, *, status=200, content=b'', headers=None):
        self.data, self.status_code, self.content, self.headers = data, status, content, headers or {}
    def json(self): return self.data


class PrintClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {
            'MOYSKLAD_TOKEN':'test-ms', 'OZON_CLIENT_ID_ACC1':'test-client-1','OZON_CLIENT_ID_ACC2':'test-client-2',
            'OZON_API_KEY_ACC1':'test-key-1','OZON_API_KEY_ACC2':'test-key-2',
            'SHIPMENT_GUARD_WB_TOKEN_ACC1':'test-wb-1','SHIPMENT_GUARD_WB_TOKEN_ACC2':'test-wb-2',
            'YANDEX_BUSINESS_ID_ACC1':'123','YANDEX_API_KEY_ACC1':'test-market'})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def source(self, printed):
        oid, did = str(uuid4()), str(uuid4())
        org = next(k for k,v in ORGS.items() if v[0]=='2')
        agent = next(k for k,v in AGENTS.items() if v=='ozon')
        demand_meta = {'type':'demand','href':MS+'/entity/demand/'+did}
        order = {'id':oid,'name':'123-0001-1','demands':[{'meta':demand_meta}],
                 'organization':{'meta':{'href':MS+'/entity/organization/'+org}},
                 'agent':{'meta':{'href':MS+'/entity/counterparty/'+agent}},
                 'state':{'name':'Собирается'},'deliveryPlannedMoment':'2026-11-03 12:00:00'}
        demand = {'id':did,'meta':demand_meta,'printed':printed,
                  'customerOrder':{'meta':{'href':MS+'/entity/customerorder/'+oid}}}
        return order,demand

    def test_test_export_blocks_unprinted_without_post(self):
        order,demand = self.source(False); calls=[]
        def request(method,url,**kwargs):
            calls.append(method)
            if '/customtemplate/' in url:return Response({'name':TEMPLATE_NAME,'meta':{'type':'customtemplate'}})
            return Response(order if '/customerorder/' in url else demand)
        clients=PrintClients(audit_root=self.root/'raw',request=request,delays=False)
        with self.assertRaises(PrintBlocked):
            clients.export_receipt(order['id'],demand['id'],self.root,expected_account='oz_acc2',expected_number=order['name'])
        self.assertNotIn('POST',calls)

    def test_single_order_reads_expand_state_without_mutating_query(self):
        oid=str(uuid4());queries=[]
        def request(method,url,**kwargs):
            queries.append(kwargs.get('params'))
            state={'meta':{'href':MS+'/entity/customerorder/metadata/states/'+str(uuid4())}}
            if 'state' in (kwargs.get('params') or {}).get('expand','').split(','):state['name']='Собирается'
            return Response({'id':oid,'state':state})
        clients=PrintClients(audit_root=self.root/'raw',request=request,delays=False)
        result=clients.ms_get('/entity/customerorder/'+oid)
        self.assertEqual(result['state']['name'],'Собирается')
        self.assertEqual(queries[-1],{'expand':'state'})
        query={'expand':'store,agent'}
        clients.ms_get('/entity/customerorder/'+oid,params=query)
        self.assertEqual(query,{'expand':'store,agent'})
        self.assertEqual(queries[-1],{'expand':'store,agent,state'})

    def test_genuinely_changed_order_status_still_blocks_export(self):
        order,demand=self.source(True);order['state']['name']='Отгружен';calls=[]
        def request(method,url,**kwargs):
            calls.append(method)
            if '/customtemplate/' in url:return Response({'name':TEMPLATE_NAME,'meta':{'type':'customtemplate'}})
            return Response(order if '/customerorder/' in url else demand)
        clients=PrintClients(audit_root=self.root/'raw',request=request,delays=False)
        with self.assertRaisesRegex(PrintBlocked,'order_changed_since_counter_click'):
            clients.export_receipt(order['id'],demand['id'],self.root,expected_account='oz_acc2',
                expected_number=order['name'],expected_day=date(2026,11,3))
        self.assertNotIn('POST',calls)

    def test_faster_read_budget_does_not_accelerate_label_methods(self):
        clock=[100.0];starts=[]
        def request(*args,**kwargs):
            starts.append(clock[0]);return Response({})
        def sleep(seconds):clock[0]+=seconds
        clients=PrintClients(audit_root=self.root/'raw',request=request)
        clients._next={}
        with patch('print_service.clients.time.monotonic',side_effect=lambda:clock[0]), \
                patch('print_service.clients.time.sleep',side_effect=sleep):
            for _ in range(4):
                clients._call('ozon','oz_acc1','POST','https://api-seller.ozon.ru/v3/posting/fbs/get')
            for _ in range(2):
                clients._call('ozon','oz_acc1','POST','https://api-seller.ozon.ru/v3/posting/fbs/package-label/create')
        self.assertEqual(starts[:4],[100,100.25,100.5,100.75])
        self.assertEqual(starts[4:],[101,102])

    def test_empty_scanit_uses_agreeing_barcode_payload_without_changing_raw_evidence(self):
        post={'posting_number':'123-0001-1','scanit':'','barcodes':{
            'upper_barcode':'855800012341000','lower_barcode':'855800012341000'}}
        c=PrintClients(audit_root=self.root/'raw',request=lambda *a,**k:Response({'result':post}),delays=False)
        result=c.ozon_posting('oz_acc2',post['posting_number'])
        self.assertEqual(result['scanit'],'855800012341000')
        self.assertEqual(result['scanit_source'],'barcodes.lower_barcode')
        self.assertEqual(post['scanit'],'')
        import json
        saved=json.loads(next((self.root/'raw').rglob('*.json')).read_text())
        self.assertEqual(saved['response']['result']['scanit'],'')

    def test_scanit_is_preserved_and_missing_code_never_guessed_from_conflicting_barcodes(self):
        for code,upper,lower,expected in (
            ('ii1234567890','855800012341000','855800012341000','ii1234567890'),
            ('','855800012341000','855800012342000',''),
            ('',None,'855800012341000',''),
            ('','invalid','invalid','')):
            with self.subTest(scanit=code,upper=upper,lower=lower):
                post={'posting_number':'123-0001-1','scanit':code,'barcodes':{'upper_barcode':upper,'lower_barcode':lower}}
                c=PrintClients(audit_root=self.root/'raw',request=lambda *a,**k:Response({'result':post}),delays=False)
                self.assertEqual(c.ozon_posting('oz_acc2',post['posting_number'])['scanit'],expected)

    def test_read_retry_is_allowed_but_uncertain_label_task_is_not_repeated(self):
        calls=[]
        def read(method,url,**kwargs):
            calls.append(url)
            if len(calls)==1:raise requests.ConnectionError('fixture')
            return Response({'result':{'posting_number':'123-0001-1'}})
        c=PrintClients(audit_root=self.root/'raw',request=read,delays=False)
        self.assertEqual(c.ozon_posting('oz_acc1','123-0001-1')['posting_number'],'123-0001-1')
        self.assertEqual(len(calls),2)
        calls.clear()
        def uncertain(method,url,**kwargs):
            calls.append(url);raise requests.ConnectionError('fixture')
        c=PrintClients(audit_root=self.root/'raw',request=uncertain,delays=False)
        with self.assertRaises(PrintBlocked):c.ozon_label('oz_acc1',{'posting_number':'123-0001-1'},self.root)
        with self.assertRaises(PrintBlocked):c.ozon_label('oz_acc1',{'posting_number':'123-0001-1'},self.root)
        self.assertEqual(len(calls),1)

    def test_export_uses_real_link_anonymous_download_and_cached_repeat(self):
        for allow,printed in ((False,True),(True,False)):
            with self.subTest(allow_unprinted=allow):
                order,demand=self.source(printed);calls=[];folder=self.root/str(allow);folder.mkdir()
                def request(method,url,**kwargs):
                    calls.append((method,url,kwargs))
                    if '/customtemplate/' in url:return Response({'name':TEMPLATE_NAME,'meta':{'type':'customtemplate','href':MS+'/entity/demand/metadata/customtemplate/'+TEMPLATE_ID}})
                    if method=='POST':return Response(status=303,headers={'Location':'https://print-prod.moysklad.ru/sample.pdf'})
                    if url.startswith('https://print-prod.'):return Response(content=pdf())
                    if '/customerorder/' in url:
                        response=dict(order,state={'meta':{'type':'state'}})
                        if 'state' in (kwargs.get('params') or {}).get('expand','').split(','):
                            response['state']=order['state']
                        return Response(response)
                    return Response(demand)
                clients=PrintClients(audit_root=folder/'raw',request=request,delays=False)
                options=dict(expected_account='oz_acc2',expected_number=order['name'],expected_day=date(2026,11,3),allow_unprinted=allow)
                raw=clients.export_receipt(order['id'],demand['id'],folder,**options)
                self.assertEqual(len(PdfReader(BytesIO(raw)).pages),1)
                download=next(call for call in calls if call[1].startswith('https://print-prod.'))
                self.assertNotIn('Authorization',download[2].get('headers',{}))
                count=len(calls);self.assertEqual(raw,clients.export_receipt(order['id'],demand['id'],folder,**options));self.assertEqual(len(calls),count)
                self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)

    def test_ozon_both_accounts_new_task_protocol_and_multiple_boxes(self):
        for account in ('oz_acc1','oz_acc2'):
            with self.subTest(account=account):
                calls=[];folder=self.root/account;folder.mkdir()
                def request(method,url,**kwargs):
                    calls.append((method,url,kwargs))
                    if url.endswith('/create'):return Response({'tasks':[{'task_id':42,'task_type':'small_label'}]})
                    if url.endswith('/get'):return Response({'status':{'code':'completed','printed_postings_count':1,'unprinted_postings':[]},'file_url':'https://ir-3.ozone.ru/label.pdf','error':''})
                    return Response(content=pdf(2))
                c=PrintClients(audit_root=folder/'raw',request=request,delays=False)
                c.ozon_label(account,{'posting_number':'123-0001-1','multi_box_qty':2},folder)
                self.assertEqual(calls[0][2]['json'],{'posting_numbers':['123-0001-1']})
                self.assertEqual(len(PdfReader(folder/'label.pdf').pages),2)
                self.assertEqual(calls[-1][2].get('headers',{}),{})
                self.assertFalse(any('/ship' in url or '/status' in url for _,url,_ in calls))

    def test_wb_both_accounts_png_pixels_and_sticker_parts(self):
        image=Image.new('1',(580,400),'white');stream=BytesIO();image.save(stream,format='PNG');encoded=base64.b64encode(stream.getvalue()).decode()
        for account in ('wb_acc1','wb_acc2'):
            with self.subTest(account=account):
                folder=self.root/account;folder.mkdir();calls=[]
                def request(method,url,**kwargs):
                    calls.append((method,url,kwargs))
                    if url.endswith('/status'):return Response({'orders':[{'id':123,'supplierStatus':'complete'}]})
                    return Response({'stickers':[{'orderId':123,'partA':'7654321','partB':'0789','barcode':'opaque','file':encoded}]})
                c=PrintClients(audit_root=folder/'raw',request=request,delays=False)
                sticker=c.wb_sticker(account,'123',folder)
                page=PdfReader(folder/'label.pdf').pages[0]
                self.assertAlmostEqual(float(page.mediabox.width)*25.4/72,58,places=3)
                self.assertEqual(page.images[0].image.convert('RGB').tobytes(),image.convert('RGB').tobytes())
                self.assertEqual(sticker['partB'],'0789')
                self.assertTrue(all(method=='POST' for method,_,_ in calls))

    def test_market_acc1_campaign_and_all_boxes_without_status_writes(self):
        calls=[]
        def request(method,url,**kwargs):
            calls.append((method,url,kwargs))
            if '/businesses/' in url:return Response({'orders':[{'orderId':456,'campaignId':789,'programType':'FBS','fake':False}]})
            if url.endswith('/data'):return Response({'result':{'orderId':456,'placesNumber':2,'parcelBoxLabels':[{'boxId':1,'fulfilmentId':'456-1'},{'boxId':2,'fulfilmentId':'456-2'}]}})
            return Response(content=pdf(2))
        c=PrintClients(audit_root=self.root/'raw',request=request,delays=False)
        c.yandex_label(c.yandex_order('456'),self.root)
        self.assertEqual(len(PdfReader(self.root/'label.pdf').pages),2)
        self.assertEqual(calls[-1][2]['params'],{'format':'A9_HORIZONTALLY'})
        self.assertTrue(all(method in ('GET','POST') for method,_,_ in calls))
        self.assertFalse(any('/status' in url or '/boxes' in url for _,url,_ in calls))
