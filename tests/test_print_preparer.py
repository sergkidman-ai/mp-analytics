# поток: print
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from uuid import uuid4

from pypdf import PdfWriter

from print_service.domain import PrintBlocked
from print_service.preparer import Preparer, LEGACY_RENDER_PROFILES, fingerprint, validate_native_label
from print_service.clients import PrintClients
from print_service.storage import save_json


class NativeLabelTests(unittest.TestCase):
    def test_legacy_export_is_reused_without_network_for_a_new_rendering_profile(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);member=dict(ms_order_id=str(uuid4()),ms_demand_id=str(uuid4()))
            source=dict(profile='new-rendering',order={'id':member['ms_order_id']},demand={'id':member['ms_demand_id']},positions=[])
            previous=root/'receipts'/fingerprint(dict(source,profile=LEGACY_RENDER_PROFILES[0]));previous.mkdir(parents=True)
            writer=PdfWriter();writer.add_blank_page(width=612,height=792);writer.write(previous/'receipt_ms.pdf')
            save_json(previous/'ms_export_intent.json',dict(order_id=member['ms_order_id'],demand_id=member['ms_demand_id'],state='sent'))
            target=root/'receipt_sources'/'new';target.mkdir(parents=True)
            Preparer(object(),object(),root=root)._reuse_export(root,target,source,member)
            def no_network(*a,**k):raise AssertionError('Cached export must not call MS')
            client=PrintClients(audit_root=root/'raw',request=no_network,delays=False)
            raw=client.export_receipt(member['ms_order_id'],member['ms_demand_id'],target,expected_account='oz_acc2',expected_number='123-0001-1')
            self.assertEqual(raw,(previous/'receipt_ms.pdf').read_bytes())
            self.assertTrue((target/'imported_from.json').exists())

    def test_uncertain_legacy_export_journal_is_preserved_and_never_reposted(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);member=dict(ms_order_id=str(uuid4()),ms_demand_id=str(uuid4()))
            source=dict(profile='new',order={'id':member['ms_order_id']},demand={'id':member['ms_demand_id']},positions=[])
            previous=root/'receipts'/fingerprint(dict(source,profile=LEGACY_RENDER_PROFILES[0]));previous.mkdir(parents=True)
            save_json(previous/'ms_export_intent.json',dict(order_id=member['ms_order_id'],demand_id=member['ms_demand_id'],state='sent'))
            target=root/'receipt_sources'/'new';target.mkdir(parents=True)
            Preparer(object(),object(),root=root)._reuse_export(root,target,source,member)
            def no_network(*a,**k):raise AssertionError('Unknown export must not be repeated')
            client=PrintClients(audit_root=root/'raw',request=no_network,delays=False)
            with self.assertRaisesRegex(PrintBlocked,'ms_export_result_uncertain'):
                client.export_receipt(member['ms_order_id'],member['ms_demand_id'],target,expected_account='oz_acc2',expected_number='123-0001-1')

    def test_rotated_native_yandex_label_is_accepted_without_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'label.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=113, height=164).rotate(90)
            writer.write(path)
            before = path.read_bytes()
            validate_native_label(path)
            self.assertEqual(path.read_bytes(), before)

    def test_unexpected_size_is_rejected_without_resizing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'label.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.write(path)
            before = path.read_bytes()
            with self.assertRaises(PrintBlocked): validate_native_label(path)
            self.assertEqual(path.read_bytes(), before)
