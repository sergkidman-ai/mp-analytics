# поток: print
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from pypdf import PdfWriter

from print_service.clients import MS, PrintClients
from print_service.domain import PrintBlocked
from print_service.labels import LabelPrefetcher
from print_service.preparer import Preparer
from print_service.storage import save_json
from shipment_guard.config import ORGS, AGENTS

NOW = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)


class SavedLabels:
    lease_check = staticmethod(lambda: None)

    def __init__(self, platform):
        self.platform, self.downloads, self.exports, self.reads = platform, 0, 0, []
        oid, did = str(uuid4()), str(uuid4())
        org = next(k for k, v in ORGS.items() if v[0] == '1')
        agent = next(k for k, v in AGENTS.items() if v == platform)
        number = '123-0001-1' if platform == 'ozon' else '123456'
        account = {'ozon':'oz_acc1', 'wb':'wb_acc1', 'yandex':'ya_acc1'}[platform]
        meta = dict(type='demand', href=MS+'/entity/demand/'+did)
        self.order = dict(id=oid, name=number, printed=False, state={'name':'Собирается'},
                          deliveryPlannedMoment='2026-11-05 12:00:00', demands=[{'meta':meta}],
                          organization={'meta':{'href':MS+'/entity/organization/'+org}},
                          agent={'meta':{'href':MS+'/entity/counterparty/'+agent}})
        self.demand = dict(id=did, meta=meta, printed=False,
                           customerOrder={'meta':{'href':MS+'/entity/customerorder/'+oid}})
        self.row = dict(id=oid, demand_id=did, account=account, external_id=number)

    def ms_get(self, path, params=None):
        self.reads.append(path)
        if path.endswith('/positions'):
            return {'meta':{'size':1}, 'rows':[{'quantity':1, 'assortment':{'code':'SKU'}}]}
        return deepcopy(self.order if '/customerorder/' in path else self.demand)

    def ozon_posting(self, account, number):
        return dict(posting_number=number, status='awaiting_deliver', scanit='ii1234567890',
                    delivery_method={'warehouse_id':123}, multi_box_qty=1)

    def yandex_order(self, number):
        return dict(id=int(number), status='PROCESSING', campaignId=123)

    def wb_status(self, account, number): pass

    def _label(self, folder):
        self.downloads += 1
        writer = PdfWriter()
        if self.platform == 'yandex': writer.add_blank_page(width=113, height=164).rotate(90)
        else: writer.add_blank_page(width=164.25, height=113.25)
        writer.write(folder/'label.pdf')

    def ozon_label(self, account, post, folder): self._label(folder)
    def yandex_label(self, post, folder): self._label(folder)
    def wb_sticker(self, account, number, folder):
        self._label(folder)
        save_json(folder/'label_source.json', dict(orderId=int(number), partA='123456', partB='7890'))

    def export_receipt(self, oid, did, folder, **kwargs):
        if (folder/'receipt_ms.pdf').exists():return (folder/'receipt_ms.pdf').read_bytes()
        self.exports += 1
        self.demand['printed'] = True
        writer = PdfWriter();writer.add_blank_page(width=595.276, height=841.89)
        path = folder/'receipt_ms.pdf';writer.write(path)
        return path.read_bytes()


class SavedQueue:
    def __init__(self): self.batch_id = None;self.labels=[];self.artifacts=[]
    def order(self, oid): return dict(batch_id=self.batch_id)
    def mark_prefetched_label(self, oid, **kwargs): self.labels.append((oid,kwargs));return True
    def resolve_label_errors(self, oid, **kwargs): return 0
    def mark_artifact(self, oid, **kwargs): self.artifacts.append((oid,kwargs))


class LabelPrefetchTests(unittest.TestCase):
    def test_express_label_cache_without_scanit_is_reused_when_counter_exports_receipt(self):
        from fpdf import FPDF
        for status,tail in (('delivering','1234'),('awaiting_registration',None)):
            with self.subTest(status=status),TemporaryDirectory() as directory:
                clients,queue=SavedLabels('ozon'),SavedQueue();number=clients.row['external_id']
                post=dict(posting_number=number,status=status,is_express=True,
                    available_actions=['label_download_small'],scanit=None,multi_box_qty=1,
                    delivery_method={'warehouse_id':123})
                pdf=FPDF(unit='pt',format=(164,114));pdf.set_auto_page_break(False);pdf.add_page()
                pdf.set_font('Helvetica',size=9);pdf.text(5,20,number)
                if tail:pdf.set_font('Helvetica',style='B',size=24);pdf.text(5,50,tail)
                native=bytes(pdf.output())
                def download(account,post,folder):
                    clients.downloads+=1;(folder/'label.pdf').write_bytes(native)
                with patch.object(clients,'ozon_posting',return_value=post),patch.object(clients,'ozon_label',side_effect=download):
                    self.assertTrue(LabelPrefetcher(queue,clients,root=directory).one(clients.row,now=NOW))
                    self.assertEqual(clients.exports,0)
                    post['available_actions']=[]
                    self.assertTrue(LabelPrefetcher(queue,clients,root=directory).one(clients.row,now=NOW))
                    member=dict(account=clients.row['account'],external_id=number,
                        ms_order_id=clients.row['id'],ms_demand_id=clients.row['demand_id'])
                    with patch('print_service.preparer.annotate_receipt',side_effect=lambda data,**kwargs:data) as annotate:
                        result=Preparer(queue,clients,root=directory,allow_unprinted=True)._one(member,NOW.date())
                    self.assertEqual(annotate.call_args.kwargs['code'],number)
                    self.assertEqual(annotate.call_args.kwargs['last_four'],tail)
                    self.assertFalse(annotate.call_args.kwargs['split_code_tail'])
                    self.assertEqual(annotate.call_args.kwargs['color']['short'],'ЭКС')
                self.assertEqual(clients.downloads,1);self.assertEqual(clients.exports,1)
                self.assertEqual(Path(result.label_files[0]).read_bytes(),native)

    def test_only_labels_are_downloaded_and_native_files_reused_for_all_platforms(self):
        for platform in ('ozon','wb','yandex'):
            with self.subTest(platform=platform), TemporaryDirectory() as directory:
                clients, queue = SavedLabels(platform), SavedQueue()
                warmer = LabelPrefetcher(queue,clients,root=directory)
                self.assertTrue(warmer.one(clients.row,now=NOW))
                original = queue.labels[-1][1]['path'].read_bytes()
                self.assertTrue(warmer.one(clients.row,now=NOW))
                self.assertEqual(queue.labels[-1][1]['path'].read_bytes(),original)
                self.assertEqual(clients.downloads,1);self.assertEqual(clients.exports,0)
                self.assertFalse(clients.order['printed']);self.assertFalse(clients.demand['printed'])
                self.assertTrue(all('/customerorder/' in path for path in clients.reads))
                self.assertFalse(list(Path(directory).rglob('receipts')))

    def test_counter_preparation_uses_background_label_and_only_then_exports_receipt(self):
        with TemporaryDirectory() as directory:
            clients, queue = SavedLabels('wb'), SavedQueue()
            LabelPrefetcher(queue,clients,root=directory).one(clients.row,now=NOW)
            label = queue.labels[0][1]['path'];before=label.read_bytes()
            member = dict(account=clients.row['account'],external_id=clients.row['external_id'],
                          ms_order_id=clients.row['id'],ms_demand_id=clients.row['demand_id'])
            self.assertEqual(clients.exports,0)
            with patch('print_service.preparer.annotate_receipt',side_effect=lambda data,**kwargs:data):
                result=Preparer(queue,clients,root=directory,allow_unprinted=True)._one(member,NOW.date())
            self.assertEqual(clients.exports,1);self.assertEqual(clients.downloads,1)
            self.assertEqual(Path(result.label_files[0]).read_bytes(),before)
            self.assertTrue(clients.demand['printed'])

    def test_claimed_or_changed_orders_are_not_downloaded(self):
        with TemporaryDirectory() as directory:
            clients, queue = SavedLabels('wb'), SavedQueue()
            warmer=LabelPrefetcher(queue,clients,root=directory)
            queue.batch_id=str(uuid4())
            self.assertFalse(warmer.one(clients.row,now=NOW));self.assertEqual(clients.reads,[])
            queue.batch_id=None;clients.order['state']['name']='Отгружен'
            with self.assertRaises(PrintBlocked): warmer.one(clients.row,now=NOW)
            self.assertEqual(clients.downloads,0);self.assertEqual(clients.exports,0)

    def test_rendering_change_reuses_the_same_source_receipt_export(self):
        with TemporaryDirectory() as directory:
            clients,queue=SavedLabels('wb'),SavedQueue()
            member=dict(account=clients.row['account'],external_id=clients.row['external_id'],
                        ms_order_id=clients.row['id'],ms_demand_id=clients.row['demand_id'])
            preparer=Preparer(queue,clients,root=directory,allow_unprinted=True)
            with patch('print_service.preparer.annotate_receipt',side_effect=lambda data,**kwargs:data):
                first=preparer._one(member,NOW.date())
                with patch('print_service.preparer.profile_key',return_value='another-rendering-profile'):
                    second=preparer._one(member,NOW.date())
            self.assertEqual(clients.exports,1)
            self.assertNotEqual(first.receipt_files,second.receipt_files)
            self.assertEqual(Path(first.receipt_files[0]).read_bytes(),Path(second.receipt_files[0]).read_bytes())

    def test_empty_scanit_qr_payload_is_shared_by_prefetch_and_foreground_receipt(self):
        with TemporaryDirectory() as directory:
            clients,queue=SavedLabels('ozon'),SavedQueue()
            code='855800012341000'
            post=dict(posting_number=clients.row['external_id'],status='awaiting_deliver',scanit='',
                      barcodes=dict(upper_barcode=code,lower_barcode=code),multi_box_qty=1,
                      delivery_method={'warehouse_id':123})
            class Response:
                status_code=200
                def json(self):return {'result':post}
            reader=PrintClients(audit_root=Path(directory)/'raw',request=lambda *a,**k:Response(),delays=False)
            clients.ozon_posting=reader.ozon_posting
            LabelPrefetcher(queue,clients,root=directory).one(clients.row,now=NOW)
            label=queue.labels[0][1]['path'];before=label.read_bytes()
            self.assertEqual(clients.exports,0);self.assertFalse(clients.demand['printed'])
            member=dict(account=clients.row['account'],external_id=clients.row['external_id'],
                        ms_order_id=clients.row['id'],ms_demand_id=clients.row['demand_id'])
            with patch('print_service.preparer.warehouse_color',return_value={'short':'Чрн','css':'#000000'}), \
                 patch('print_service.preparer.annotate_receipt',side_effect=lambda data,**kwargs:data) as annotate:
                result=Preparer(queue,clients,root=directory,allow_unprinted=True)._one(member,NOW.date())
            self.assertEqual(annotate.call_args.kwargs['code'],code)
            self.assertEqual(clients.downloads,1);self.assertEqual(clients.exports,1)
            self.assertEqual(Path(result.label_files[0]).read_bytes(),before)

    def test_failed_background_order_does_not_stop_other_labels_or_create_receipts(self):
        from print_service.service import PrintService
        with TemporaryDirectory() as directory:
            clients,queue=SavedLabels('wb'),SavedQueue()
            service=PrintService(queue=queue,clients=clients,root=directory,clock=lambda:NOW)
            clients.order['state']['name']='Отгружен'
            service.run_label_prefetch(clients.row)
            clients.order['state']['name']='Собирается'
            service.run_label_prefetch(clients.row)
            self.assertEqual(len(queue.labels),1);self.assertEqual(clients.exports,0)
            self.assertEqual(len(list((Path(directory)/'errors').glob('*.json'))),1)

    def test_ozon_before_assembly_does_not_create_label_tasks(self):
        with TemporaryDirectory() as directory:
            clients,queue=SavedLabels('ozon'),SavedQueue()
            with patch.object(clients,'ozon_posting',return_value={'status':'awaiting_packaging'}):
                with self.assertRaises(PrintBlocked):
                    LabelPrefetcher(queue,clients,root=directory).one(clients.row,now=NOW)
            self.assertEqual(clients.downloads,0);self.assertEqual(clients.exports,0)
