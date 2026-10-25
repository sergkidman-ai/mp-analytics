# поток: ret
"""Real DOM and QR-decoder integration; WB pages are local fixtures, never live accounts."""
import base64
import io
import json
from pathlib import Path
import unittest

import qrcode
from playwright.sync_api import sync_playwright

EXTENSION=Path(__file__).resolve().parents[1]/'tools/wb_buyer_windows/WB_QR_Extension'

class WbQrAutoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(channel='chrome',headless=True,args=['--no-sandbox'])
        buf=io.BytesIO();qrcode.make('public-auto-test',box_size=6).save(buf,format='PNG')
        cls.png='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
        cls.extract=(EXTENSION/'extract.js').read_text()
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.page=self.browser.new_page()
        self.page.route('https://www.wildberries.ru/**',lambda r:r.fulfill(body='<html><body></body></html>',content_type='text/html'))
        self.page.goto('https://www.wildberries.ru/lk/fixture')
    def tearDown(self):self.page.close()
    def fixture(self,canvas=False):
        asset='<canvas id="qr" width="198" height="198"></canvas>' if canvas else f'<img width="198" height="198" src="{self.png}">'
        self.page.set_content(f'''<button id="open" onclick="document.getElementById('modal').style.display='block'">QR-код</button>
        <button id="purchase" onclick="window.unwanted=true">Оплатить</button>
        <div id="modal" role="dialog" style="display:none"><p>Покажите QR-код в пункте выдачи</p>
        {asset}<p>Или назовите код 123 456</p><p>Коды действуют только 23 октября до 00:00 по мск</p></div>''')
        if canvas:self.page.evaluate('''async png=>{const i=new Image();i.src=png;await i.decode();document.getElementById('qr').getContext('2d').drawImage(i,0,0,198,198);}''',self.png)
    def test_auto_opens_modal_and_extracts_original_image_and_code(self):
        self.fixture();r=self.page.evaluate(self.extract)
        self.assertTrue(r['ok']);self.assertTrue(r['diagnostics']['clickedQr'])
        self.assertEqual(r['candidates'][0]['code'],'123456')
        self.assertIn('00:00',r['candidates'][0]['validity'])
        self.assertFalse(self.page.evaluate('Boolean(window.unwanted)'))
    def test_canvas_is_extracted_without_visible_tab_screenshot(self):
        self.fixture(canvas=True);r=self.page.evaluate(self.extract)
        self.assertTrue(r['ok']);self.assertTrue(r['candidates'][0]['png'].startswith('data:image/png'))
    def test_already_open_modal_does_not_claim_auto_open(self):
        self.fixture();self.page.locator('#open').click();r=self.page.evaluate(self.extract)
        self.assertTrue(r['ok']);self.assertFalse(r['diagnostics']['clickedQr'])
    def test_ambiguous_buttons_stop_without_clicking(self):
        self.page.set_content('<button onclick="window.clicked=true">QR-код</button><button onclick="window.clicked=true">Получить QR-код</button>')
        r=self.page.evaluate(self.extract)
        self.assertEqual(r['reason'],'ambiguous_qr_controls');self.assertFalse(self.page.evaluate('Boolean(window.clicked)'))
    def test_login_is_rejected_without_exposing_page_text(self):
        self.page.set_content('<input type="tel" value="private_phone"><p>private_order_secret</p>')
        r=self.page.evaluate(self.extract)
        self.assertEqual(r['reason'],'login_or_protection')
        self.assertNotIn('private_phone',json.dumps(r));self.assertNotIn('private_order_secret',json.dumps(r))
    def test_safe_orders_link_discovered_instead_of_guessed(self):
        self.page.set_content('<a href="/lk/safe-orders">Заказы</a>')
        r=self.page.evaluate(self.extract)
        self.assertEqual(r['reason'],'navigate_orders');self.assertEqual(r['navigate'],'https://www.wildberries.ru/lk/safe-orders')
    def test_unrelated_or_external_link_never_clicked(self):
        self.page.set_content('<a href="https://example.com/lk/orders">Заказы</a><button onclick="window.clicked=true">Украсить QR-код</button>')
        r=self.page.evaluate(self.extract)
        self.assertEqual(r['reason'],'qr_control_not_found');self.assertFalse(self.page.evaluate('Boolean(window.clicked)'))
    def test_runner_formats_cropped_code_and_keeps_report_private(self):
        self.fixture();extracted=self.page.evaluate(self.extract)
        self.page.route('https://runner.invalid/**',lambda r:r.fulfill(path=str(EXTENSION/('runner.html' if r.request.url.split('?')[0].endswith('/') else r.request.url.rsplit('/',1)[-1].split('?')[0]))))
        self.page.add_init_script('''window.chrome={tabs:{get:async()=>({id:1,url:'https://www.wildberries.ru/lk/fixture',status:'complete'})},scripting:{executeScript:async()=>[{result:window.fixtureResult}]}}''')
        self.page.goto('https://runner.invalid/?tab=1')
        # The runner starts immediately: install the result before loading via a second init script.
        self.page.add_init_script('window.fixtureResult='+json.dumps(extracted))
        self.page.reload();self.page.wait_for_function('!document.getElementById("report").hidden')
        self.assertFalse(self.page.locator('#save').is_hidden())
        self.assertIn('открыто автоматически',self.page.locator('#status').inner_text())
        report=self.page.evaluate('async()=>await(await fetch(document.getElementById("report").href)).json()')
        self.assertTrue(report['success']);self.assertTrue(report['numericCodeFound'])
        self.assertNotIn('123456',json.dumps(report));self.assertNotIn('data:image',json.dumps(report))
        self.assertNotIn('public-auto-test',json.dumps(report))
    def test_manifest_access_is_limited_to_buyer_wb(self):
        m=json.loads((EXTENSION/'manifest.json').read_text())
        self.assertEqual(m['host_permissions'],['https://www.wildberries.ru/*','https://wildberries.ru/*','https://bi.metaverseworld.ru/api/returns/wb-qr/diskver'])
        self.assertEqual(m['permissions'],['activeTab','scripting','alarms','storage'])
        self.assertEqual(m['background'],{'service_worker':'background.js'});self.assertNotIn('content_scripts',m)

if __name__=='__main__':unittest.main()
