# поток: ret
"""Popup workflow tests with mocked Chrome API; no real buyer session is used."""
import base64
import io
import json
from pathlib import Path
import unittest

from PIL import Image, ImageDraw
import qrcode
from playwright.sync_api import sync_playwright

EXTENSION = Path(__file__).resolve().parents[1] / 'tools/wb_buyer_windows/WB_QR_Extension'


class WbQrExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route('https://local-extension-test.invalid/**', lambda route: route.fulfill(
            path=str(EXTENSION / ('popup.html' if route.request.url.endswith('/') else route.request.url.rsplit('/', 1)[-1]))))
        image = io.BytesIO()
        screen = Image.new('RGB', (1000, 650), '#eeeeee')
        qr = qrcode.make('public-wb-test-fixture', box_size=6).convert('RGB')
        screen.paste(qr, (380, 120))
        ImageDraw.Draw(screen).text((450, 120 + qr.height + 8), '123 456', fill='black')
        screen.save(image, format='PNG')
        self.png_url = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        self.page.add_init_script('''
          window.fixture = {tab:{id:1,windowId:7,url:'https://www.wildberries.ru/lk'}, calls:0};
          window.chrome = {runtime:{lastError:null},tabs:{
            query:(_query,callback)=>callback([window.fixture.tab]),
            captureVisibleTab:(windowId,options,callback)=>{
              window.fixture.calls++; window.fixture.format=options.format;
              callback(window.fixture.png);
            }
          }};
        ''')
        self.page.goto('https://local-extension-test.invalid/')
        self.page.evaluate('(png)=>window.fixture.png=png', self.png_url)

    def tearDown(self):
        self.page.close()

    def test_permissions_are_limited_to_capture_and_wb_dom_test(self):
        manifest = json.loads((EXTENSION / 'manifest.json').read_text())
        self.assertEqual(manifest['permissions'], ['activeTab', 'scripting', 'alarms', 'storage'])
        self.assertEqual(manifest['host_permissions'], ['https://www.wildberries.ru/*', 'https://wildberries.ru/*', 'https://bi.metaverseworld.ru/api/returns/wb-qr/diskver'])
        for key in ['content_scripts']:
            self.assertNotIn(key, manifest)
        self.assertIn("connect-src https://bi.metaverseworld.ru", manifest['content_security_policy']['extension_pages'])

    def test_user_click_crops_png_and_preserves_qr_payload(self):
        self.assertEqual(self.page.evaluate('fixture.calls'), 0)
        self.page.locator('#capture').click()
        self.page.wait_for_function('!document.getElementById("download").hidden')
        result = self.page.locator('#download').get_attribute('href')
        self.assertNotEqual(result, self.png_url)
        im = Image.open(io.BytesIO(base64.b64decode(result.split(',')[1])))
        self.assertEqual(im.format, 'PNG')
        self.assertGreaterEqual(im.width, 600)
        payload = self.page.evaluate('''async data => {const i=new Image();i.src=data;await i.decode();
          const c=document.createElement('canvas');c.width=i.width;c.height=i.height;
          const x=c.getContext('2d');x.drawImage(i,0,0);const p=x.getImageData(0,0,c.width,c.height);
          return jsQR(p.data,p.width,p.height).data;}''',result)
        self.assertEqual(payload,'public-wb-test-fixture')
        self.assertEqual(self.page.evaluate('fixture.format'), 'png')
        self.assertEqual(self.page.evaluate('fixture.calls'), 1)

    def test_other_sites_are_not_captured_and_old_preview_is_cleared(self):
        self.page.locator('#capture').click()
        self.page.wait_for_function('!document.getElementById("download").hidden')
        self.page.evaluate("fixture.tab.url='https://www.wildberries.ru.example.com/'")
        self.page.locator('#capture').click()
        self.page.wait_for_function('!document.getElementById("capture").disabled')
        self.assertEqual(self.page.evaluate('fixture.calls'), 1)
        self.assertTrue(self.page.locator('#download').is_hidden())
        self.assertIsNone(self.page.locator('#download').get_attribute('href'))

    def test_blank_screenshot_does_not_save_a_full_page(self):
        image = io.BytesIO()
        Image.new('RGB', (600, 400), 'white').save(image, format='PNG')
        data = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        self.page.evaluate('(png)=>fixture.png=png', data)
        self.page.locator('#capture').click()
        self.page.wait_for_function('!document.getElementById("capture").disabled')
        self.assertTrue(self.page.locator('#download').is_hidden())
        self.assertIn('QR не найден', self.page.locator('#status').inner_text())

    def test_switching_tabs_cancels_image(self):
        self.page.evaluate('''()=>{chrome.tabs.captureVisibleTab=(id,opts,callback)=>{
          fixture.calls++; fixture.tab={id:2,windowId:7,url:'https://www.wildberries.ru/lk'};
          callback(fixture.png);
        }}''')
        self.page.locator('#capture').click()
        self.page.wait_for_function('!document.getElementById("capture").disabled')
        self.assertTrue(self.page.locator('#download').is_hidden())
        self.assertIn('переключилась', self.page.locator('#status').inner_text())


if __name__ == '__main__':
    unittest.main()
