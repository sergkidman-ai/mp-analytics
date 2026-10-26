# поток: ret
"""Prevent cross-company QR uploads, caches, captions and daily attachments."""
import base64
import io
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qrcode
from fastapi.testclient import TestClient
from returns_bot import wb_qr, wb_qr_ingest, daily_push, orgs

class CompanyQrTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.digital=Path(self.tmp.name)/'digital';self.diskver=Path(self.tmp.name)/'diskver'
        for attribute,path in [('CACHE',self.diskver),('CACHE_DIGITAL',self.digital)]:
            p=patch.object(wb_qr,attribute,path);p.start();self.addCleanup(p.stop)
        p=patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,26,9,tzinfo=wb_qr.MSK));p.start();self.addCleanup(p.stop)
        p=patch.object(wb_qr_ingest,'token',side_effect=lambda a='wb_acc2':{'wb_acc1':'digital-test-token','wb_acc2':'diskver-test-token'}[a]);p.start();self.addCleanup(p.stop)
        self.client=TestClient(wb_qr_ingest.app)
    def png(self,payload):
        b=io.BytesIO();qrcode.make(payload,box_size=8).save(b,format='PNG');return b.getvalue()
    def post(self,company,credential,payload):
        data={'day':'2026-10-26','validity':'Коды действуют только 26 октября до 00:00 по мск','png_b64':base64.b64encode(self.png(payload)).decode()}
        return self.client.post('/api/returns/wb-qr/'+company,json=data,headers={'Authorization':'Bearer '+credential})
    def test_tokens_cannot_write_the_other_company(self):
        self.assertEqual(self.post('tsifrovoy','diskver-test-token','synthetic').status_code,401)
        self.assertEqual(self.post('diskver','digital-test-token','synthetic').status_code,401)
        self.assertFalse(self.digital.exists());self.assertFalse(self.diskver.exists())
    def test_real_qr_validation_and_caches_are_independent(self):
        self.assertEqual(self.post('diskver','diskver-test-token','fixture-diskver').status_code,200)
        self.assertEqual(self.post('tsifrovoy','digital-test-token','fixture-digital').status_code,200)
        digital=wb_qr.current(account='wb_acc1');diskver=wb_qr.current(account='wb_acc2')
        self.assertEqual(digital[1]['account'],'wb_acc1');self.assertEqual(diskver[1]['account'],'wb_acc2')
        self.assertNotEqual(digital[1]['qr_sha256'],diskver[1]['qr_sha256'])
        self.assertEqual(wb_qr.attachment(account='wb_acc1')[0],'wb-qr-tsifrovoy-2026-10-26.png')
        self.assertTrue(wb_qr.attachment(account='wb_acc1')[2].startswith('Wildberries - Цифровой\n'))
        self.assertEqual(wb_qr.attachment(account='wb_acc2')[0],'wb-qr-diskver-2026-10-26.png')
    def test_new_company_joins_reports_after_first_qr(self):
        self.assertEqual(wb_qr.enabled_accounts(),('wb_acc2',))
        self.post('tsifrovoy','digital-test-token','fixture-digital')
        self.assertEqual(wb_qr.enabled_accounts(),('wb_acc1','wb_acc2'))
    def test_wrong_account_metadata_is_rejected(self):
        self.post('tsifrovoy','digital-test-token','fixture-digital')
        p=self.digital/'2026-10-26.json';d=json.loads(p.read_text());d['account']='wb_acc2';p.write_text(json.dumps(d))
        self.assertIsNone(wb_qr.current(account='wb_acc1'))
    def test_pairing_pages_expose_only_their_own_scoped_token(self):
        h={'X-Pult-User':'fixture-user'}
        digital=self.client.get('/returns/wb-qr/setup/tsifrovoy',headers=h)
        self.assertIn('Цифровой',digital.text);self.assertIn('digital-test-token',digital.text);self.assertNotIn('diskver-test-token',digital.text)
        diskver=self.client.get('/returns/wb-qr/setup',headers=h)
        self.assertIn('diskver-test-token',diskver.text);self.assertNotIn('digital-test-token',diskver.text)
        self.assertEqual(self.client.get('/returns/wb-qr/setup/tsifrovoy').status_code,403)
    def test_unknown_company_is_rejected(self):
        self.assertEqual(self.post('unknown','digital-test-token','fixture').status_code,404)

class CompanyReportTests(unittest.TestCase):
    def pass_qr(self, account, chat, attachment):
        name,data,caption,mime=attachment
        return daily_push.tg.send_document(chat,data,name,caption,mime=mime)

    def setUp(self):
        p=patch.object(daily_push.wb_qr_delivery,'send',side_effect=self.pass_qr)
        p.start();self.addCleanup(p.stop)

    def test_each_qr_follows_its_own_summary_for_both_users(self):
        events=[]
        def attach(*,account):return (account+'.png',b'PNG',account,'image/png')
        with patch.object(daily_push.render,'summaries',return_value=[(orgs.DIGITAL,'digital-summary'),(orgs.DISQUARE,'diskver-summary')]),patch.object(daily_push,'barcode_photos',return_value=[]),patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc1','wb_acc2')),patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,26,9,tzinfo=wb_qr.MSK)),patch.object(wb_qr,'attachment',side_effect=attach),patch.object(daily_push.tg,'send',side_effect=lambda chat,text,**kw:events.append((chat,'text',text))),patch.object(daily_push.tg,'send_document',side_effect=lambda chat,data,name,*a,**kw:events.append((chat,'file',name))):
            self.assertEqual(daily_push.run(['fixture-user1','fixture-user2'],skip_collect=True),0)
        expected=[]
        for user in ['fixture-user1','fixture-user2']:
            expected.extend([(user,'text','digital-summary'),(user,'file','wb_acc1.png'),(user,'text','diskver-summary'),(user,'file','wb_acc2.png')])
        self.assertEqual(events,expected)
    def test_digital_extension_has_only_digital_upload_and_pairing_links(self):
        base=Path(__file__).resolve().parents[1]/'tools/wb_buyer_windows_digital/WB_QR_Extension'
        manifest=json.loads((base/'manifest.json').read_text())
        self.assertEqual(manifest['name'],'QR WB — Цифровой')
        self.assertIn('https://bi.metaverseworld.ru/api/returns/wb-qr/tsifrovoy',manifest['host_permissions'])
        self.assertNotIn('https://bi.metaverseworld.ru/api/returns/wb-qr/diskver',manifest['host_permissions'])
        self.assertIn('/returns/wb-qr/setup/tsifrovoy',(base/'popup.html').read_text())
        self.assertNotIn('/api/returns/wb-qr/diskver',(base/'background.js').read_text())

if __name__=='__main__':unittest.main()
