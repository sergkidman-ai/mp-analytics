# поток: ret
"""Moscow scheduling, authenticated ingest, cache freshness and combined daily delivery."""
import base64
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import qrcode
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from returns_bot import wb_qr, wb_qr_ingest, daily_push, orgs

EXT=Path(__file__).resolve().parents[1]/'tools/wb_buyer_windows/WB_QR_Extension'

class ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p=sync_playwright().start();cls.b=cls.p.chromium.launch(channel='chrome',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.b.close();cls.p.stop()
    def setUp(self):self.page=self.b.new_page();self.page.add_script_tag(path=str(EXT/'schedule.js'))
    def tearDown(self):self.page.close()
    def test_next_run_uses_moscow_and_skips_sunday(self):
        for now,expected in [('2026-10-23T05:59:00Z','2026-10-23T06:00:00.000Z'),('2026-10-24T06:00:00Z','2026-10-25T06:00:00.000Z'),('2026-10-31T06:00:00Z','2026-11-02T06:00:00.000Z'),('2026-10-25T12:00:00Z','2026-10-26T06:00:00.000Z')]:
            self.assertEqual(self.page.evaluate('s=>new Date(WB_QR_SCHEDULE.next(Date.parse(s))).toISOString()',now),expected)
    def test_hourly_check_uses_moscow_stops_at_midnight_and_skips_sunday(self):
        for now,expected in [
            ('2026-10-26T05:59:00Z','2026-10-26T06:00:00.000Z'),
            ('2026-10-26T06:01:00Z','2026-10-26T07:00:00.000Z'),
            ('2026-10-26T07:00:00Z','2026-10-26T08:00:00.000Z'),
            ('2026-10-26T20:59:00Z','2026-10-27T06:00:00.000Z'),
            ('2026-10-31T20:59:00Z','2026-11-02T06:00:00.000Z')]:
            self.assertEqual(self.page.evaluate('s=>new Date(WB_QR_SCHEDULE.nextCheck(Date.parse(s))).toISOString()',now),expected)

    def test_catch_up_only_today_and_once(self):
        script='''([n,last,enabled])=>WB_QR_SCHEDULE.eligible(Date.parse(n),{enabled:true,enabledAt:Date.parse(enabled)},last)'''
        self.assertTrue(self.page.evaluate(script,['2026-10-24T10:00:00Z',None,'2026-10-23T10:00:00Z']))
        self.assertFalse(self.page.evaluate(script,['2026-10-24T10:00:00Z','2026-10-24','2026-10-23T10:00:00Z']))
        self.assertTrue(self.page.evaluate(script,['2026-10-25T10:00:00Z',None,'2026-10-23T10:00:00Z']))
        self.assertFalse(self.page.evaluate(script,['2026-11-01T10:00:00Z',None,'2026-10-23T10:00:00Z']))
        self.assertFalse(self.page.evaluate(script,['2026-10-24T10:00:00Z',None,'2026-10-24T09:00:00Z']))
    def test_validity_requires_today(self):
        self.assertTrue(self.page.evaluate("WB_QR_SCHEDULE.currentValidity('Коды действуют только 23 октября до 00:00 по мск',Date.parse('2026-10-23T06:00:00Z'))"))
        self.assertFalse(self.page.evaluate("WB_QR_SCHEDULE.currentValidity('Коды действуют только 22 октября до 00:00 по мск',Date.parse('2026-10-23T06:00:00Z'))"))

class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p=sync_playwright().start();cls.b=cls.p.chromium.launch(channel='chrome',headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.b.close();cls.p.stop()
    def setUp(self):self.page=self.b.new_page();self.page.add_script_tag(path=str(EXT/'navigation.js'))
    def tearDown(self):self.page.close()
    def test_pending_wb_navigation_is_not_wrong_host(self):
        self.page.evaluate("window.calls=0;window.chrome={tabs:{get:async()=>{calls++;return calls===1?{pendingUrl:'https://www.wildberries.ru/lk/orders',status:'loading'}:{url:'https://www.wildberries.ru/lk/orders',status:'complete'};}}}")
        result=self.page.evaluate('async()=>await WB_QR_NAV.waitReady(1,1500)')
        self.assertEqual(result['status'],'complete');self.assertGreaterEqual(self.page.evaluate('calls'),2)
    def test_external_redirect_is_rejected(self):
        self.page.evaluate("window.chrome={tabs:{get:async()=>({pendingUrl:'https://other.invalid/',status:'loading'})}}")
        reason=self.page.evaluate('async()=>{try{await WB_QR_NAV.waitReady(1,1000);}catch(e){return e.message;}}')
        self.assertEqual(reason,'wrong_host')
    def test_navigation_timeout_is_distinct_from_wrong_host(self):
        self.page.evaluate("window.chrome={tabs:{get:async()=>({status:'loading'})}}")
        reason=self.page.evaluate('async()=>{try{await WB_QR_NAV.waitReady(1,10);}catch(e){return e.message;}}')
        self.assertEqual(reason,'navigation_timeout')
    def test_diagnostic_summary_excludes_path_and_query(self):
        result=self.page.evaluate("WB_QR_NAV.summary({status:'loading',pendingUrl:'https://www.wildberries.ru/private-order?secret=private-value'})")
        self.assertNotIn('private-',json.dumps(result));self.assertEqual(result['pending']['host'],'www.wildberries.ru')

class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cache=patch.object(wb_qr,'CACHE',Path(self.temp.name));self.cache.start()
        self.clock=patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,23,9,0,tzinfo=wb_qr.MSK));self.clock_mock=self.clock.start()
        self.token=patch.object(wb_qr_ingest,'token',return_value='test-only-token');self.token.start()
        self.client=TestClient(wb_qr_ingest.app)
        buf=io.BytesIO();qrcode.make('test-fixture-not-a-real-account',box_size=8).save(buf,format='PNG');self.png=buf.getvalue()
        self.body={'day':'2026-10-23','validity':'Коды действуют только 23 октября до 00:00 по мск','png_b64':base64.b64encode(self.png).decode()}
    def tearDown(self):self.token.stop();self.clock.stop();self.cache.stop();self.temp.cleanup()
    def post(self,body=None,headers=None):return self.client.post(wb_qr_ingest.UPLOAD,json=body or self.body,headers=headers or {'Authorization':'Bearer test-only-token'})
    def test_upload_and_current_day_attachment(self):
        self.assertEqual(self.post().status_code,200)
        a=wb_qr.attachment();self.assertEqual(a[1],self.png);self.assertIn('24.10 00:00',a[2])
        self.assertIsNone(wb_qr.attachment(datetime(2026,10,24,9,tzinfo=wb_qr.MSK)))
    def test_unauthorized_and_other_website_are_rejected(self):
        self.assertEqual(self.post(headers={'Authorization':'Bearer wrong'}).status_code,401)
        self.assertEqual(self.post(headers={'Authorization':'Bearer test-only-token','Origin':'https://evil.invalid'}).status_code,403)
    def test_expired_or_not_qr_is_rejected(self):
        body=dict(self.body,day='2026-10-22');self.assertEqual(self.post(body).status_code,400)
        body=dict(self.body,png_b64=base64.b64encode(b'not a PNG').decode());self.assertEqual(self.post(body).status_code,400)
    def test_sunday_is_rejected(self):
        self.clock_mock.return_value=datetime(2026,11,1,9,tzinfo=wb_qr.MSK)
        body=dict(self.body,day='2026-11-01',validity='Коды действуют только 1 ноября до 00:00 по мск')
        self.assertEqual(self.post(body).status_code,400)
    def test_explicit_sunday_test_accepts_today_qr_and_expires(self):
        self.clock_mock.return_value=datetime(2026,10,25,9,tzinfo=wb_qr.MSK)
        body=dict(self.body,day='2026-10-25',validity='Коды действуют только 25 октября до 00:00 по мск')
        self.assertEqual(self.post(body).status_code,200)
        self.assertIsNotNone(wb_qr.attachment())
        self.assertIsNone(wb_qr.attachment(datetime(2026,10,26,0,tzinfo=wb_qr.MSK)))

    def test_corrupt_cache_is_never_delivered(self):
        self.post();(Path(self.temp.name)/'2026-10-23.png').write_bytes(b'broken');self.assertIsNone(wb_qr.attachment())
    def test_pairing_page_requires_pult_auth_header(self):
        self.assertEqual(self.client.get('/returns/wb-qr/setup').status_code,403)
        response=self.client.get('/returns/wb-qr/setup',headers={'X-Pult-User':'test-user'})
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
    def test_preflight_for_extension(self):
        response=self.client.options(wb_qr_ingest.UPLOAD,headers={'Origin':'chrome-extension://'+'a'*32})
        self.assertEqual(response.status_code,204);self.assertEqual(response.headers['access-control-allow-origin'],'chrome-extension://'+'a'*32)

class DailyTests(unittest.TestCase):
    def pass_qr(self, account, chat, attachment):
        name,data,caption,mime=attachment
        return daily_push.tg.send_document(chat,data,name,caption,mime=mime)

    def setUp(self):
        p=patch.object(daily_push.wb_qr_delivery,'send',side_effect=self.pass_qr)
        p.start();self.addCleanup(p.stop)
        p=patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',))
        p.start();self.addCleanup(p.stop)

    def test_qr_is_sent_after_diskver_summary_to_all_targets(self):
        attachment=('test.png',b'PNG','QR Diskver','image/png')
        with patch.object(daily_push.render,'summaries',return_value=[(orgs.DIGITAL,'digital'),(orgs.DISQUARE,'diskver')]),patch.object(daily_push,'barcode_photos',return_value=[]),patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,23,9,tzinfo=wb_qr.MSK)),patch.object(wb_qr,'attachment',return_value=attachment),patch.object(daily_push.tg,'send') as send,patch.object(daily_push.tg,'send_document') as doc:
            self.assertEqual(daily_push.run(['test-user-1','test-user-2'],skip_collect=True),0)
            self.assertEqual(send.call_count,4);self.assertEqual(doc.call_count,2)
            self.assertEqual([c.args[0] for c in doc.call_args_list],['test-user-1','test-user-2'])
    def test_sunday_test_includes_qr_and_test_label(self):
        qr=('test.png',b'PNG','QR Diskver','image/png')
        with patch.object(daily_push.render,'summaries',return_value=[(orgs.DISQUARE,'diskver')]),patch.object(daily_push,'barcode_photos',return_value=[]),patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,25,9,tzinfo=wb_qr.MSK)),patch.object(wb_qr,'attachment',return_value=qr),patch.object(daily_push.tg,'send') as send,patch.object(daily_push.tg,'send_document') as doc:
            daily_push.run(['test-user-1','test-user-2'],skip_collect=True)
            self.assertEqual(doc.call_count,2);self.assertIn('Разовый тест',send.call_args.args[1])

    def test_only_test_sunday_runs_daily_cli(self):
        with patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,25,9,tzinfo=wb_qr.MSK)),patch.object(daily_push,'run',return_value=0) as run,patch.object(daily_push.tg,'notify_ids',return_value=['test-user']):
            self.assertEqual(daily_push.main([]),0);run.assert_called_once()
        with patch.object(wb_qr,'now_msk',return_value=datetime(2026,11,1,9,tzinfo=wb_qr.MSK)),patch.object(daily_push,'run',return_value=0) as run:
            self.assertEqual(daily_push.main([]),0);run.assert_not_called()

    def test_missing_qr_produces_warning_without_old_attachment(self):
        with patch.object(daily_push.render,'summaries',return_value=[(orgs.DISQUARE,'diskver')]),patch.object(daily_push,'barcode_photos',return_value=[]),patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,23,9,tzinfo=wb_qr.MSK)),patch.object(wb_qr,'attachment',return_value=None),patch.object(daily_push.tg,'send') as send,patch.object(daily_push.tg,'send_document') as doc:
            daily_push.run(['test-user'],skip_collect=True)
            self.assertIn('на сегодня не получен',send.call_args.args[1]);doc.assert_not_called()

if __name__=='__main__':unittest.main()
