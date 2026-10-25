# поток: ret
"""Optional integration checks: real PowerShell + local Chrome, WB network is mocked."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

PWSH = os.getenv('WB_TEST_PWSH') or shutil.which('pwsh')
SCRIPT = Path(__file__).resolve().parents[1] / 'tools/wb_buyer_windows/wb_qr_test.ps1'


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(PWSH and importlib.util.find_spec('playwright'), 'Requires pwsh and Playwright')
class WindowsQrProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.temp = tempfile.TemporaryDirectory()
        cls.pw = sync_playwright().start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.browser = cls.pw.chromium.launch_persistent_context(
            cls.temp.name + '/profile', channel='chrome', headless=True,
            viewport={'width': 900, 'height': 700},
            args=['--no-sandbox', '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={cls.port}'])
        cls.page = cls.browser.pages[0]
        cls.page.route('https://www.wildberries.ru/**', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<html><body>LOCAL TEST, NO RETURN CODE</body></html>'))
        cls.page.goto('https://www.wildberries.ru/lk/local-fixture')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.temp.cleanup()

    def run_ps(self, expression):
        return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-Command',
            f". {ps_quote(SCRIPT)} -LibraryOnly; {expression}"], capture_output=True, text=True, timeout=35)

    def capture(self, name):
        directory = Path(self.temp.name) / name
        return directory, self.run_ps(f"Save-WbScreen -Port {self.port} -OutputDirectory {ps_quote(directory)}")

    def test_captures_only_viewport_without_exporting_cookies_or_url_query(self):
        self.page.goto('https://www.wildberries.ru/lk/local-fixture?token=TEST_DO_NOT_EXPORT')
        self.browser.add_cookies([{'name': 'fixture-session', 'value': 'COOKIE_DO_NOT_EXPORT', 'domain': 'www.wildberries.ru', 'path': '/'}])
        self.page.set_content('<html><body><h1>LOCAL TEST, NO RETURN CODE</h1><div style="height:2000px">test</div></body></html>')
        directory, result = self.capture('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        from PIL import Image
        with Image.open(directory / 'WB_Diskver_screen.png') as screenshot:
            self.assertEqual(screenshot.size, (900, 700))
        report = json.loads((directory / 'test_report.json').read_text(encoding='utf-8-sig'))
        self.assertFalse(report['qr_automatically_verified'])
        self.assertFalse(report['uploaded'])
        self.assertEqual(report['host'], 'www.wildberries.ru')
        self.assertNotIn('TEST_DO_NOT_EXPORT', json.dumps(report))
        self.assertNotIn('COOKIE_DO_NOT_EXPORT', json.dumps(report))
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['WB_Diskver_screen.png', 'test_report.json'])

    def test_login_and_block_pages_do_not_produce_false_qr_files(self):
        for name, body in [('login', '<input type="tel">'), ('blocked', 'Подозрительная активность. Пожалуйста, подождите.')]:
            with self.subTest(name=name):
                self.page.set_content('<html><body>' + body + '</body></html>')
                directory, result = self.capture(name)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('форма входа' if name == 'login' else 'проверку браузера', result.stderr)
                self.assertFalse(directory.exists())

    def test_two_wb_tabs_are_rejected_as_ambiguous(self):
        self.page.set_content('<html><body>LOCAL TEST</body></html>')
        other = self.browser.new_page()
        other.route('https://www.wildberries.ru/**', lambda route: route.fulfill(status=200, body='LOCAL TEST'))
        other.goto('https://www.wildberries.ru/lk/another-local-fixture')
        try:
            directory, result = self.capture('ambiguous')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('одна вкладка', result.stderr)
            self.assertFalse(directory.exists())
        finally:
            other.close()

    def test_remote_debug_endpoint_is_rejected_before_connecting(self):
        result = self.run_ps("Invoke-LocalCdp -WebSocketUrl 'ws://example.com:9222/devtools/page/test' -Method 'Page.captureScreenshot'")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('только', result.stderr)

    def test_hidden_login_modal_does_not_block_an_open_account_screen(self):
        self.page.set_content('<html><body>LOCAL TEST<div style="display:none"><input type="tel"></div></body></html>')
        directory, result = self.capture('hidden-login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((directory / 'WB_Diskver_screen.png').exists())

    def test_white_page_is_not_saved_as_qr(self):
        self.page.set_content('<html><body></body></html>')
        directory, result = self.capture('blank')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('пустую страницу', result.stderr)
        self.assertFalse(directory.exists())

    def test_diagnostic_records_load_failures_without_account_text_or_tokens(self):
        self.page.route('https://www.wildberries.ru/broken.js*', lambda route: route.fulfill(
            status=503, content_type='application/javascript', body=''))
        self.page.goto('https://www.wildberries.ru/lk/local-fixture?token=PRIVATE_QUERY')
        self.page.set_content('<html><body>PRIVATE_ACCOUNT_TEXT<script src="https://www.wildberries.ru/broken.js?key=PRIVATE_QUERY"></script></body></html>')
        self.page.wait_for_timeout(100)
        directory = Path(self.temp.name) / 'diagnostic'
        result = self.run_ps(f"Save-WbDiagnostic -Port {self.port} -OutputDirectory {ps_quote(directory)}")
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = (directory / 'diagnostic.json').read_text(encoding='utf-8-sig')
        self.assertNotIn('PRIVATE_', raw)
        self.assertNotIn('COOKIE_DO_NOT_EXPORT', raw)
        report = json.loads(raw)
        tab = next(t for t in report['tabs'] if t['host'] == 'www.wildberries.ru')
        self.assertNotIn('probe_failed', tab)
        self.assertTrue(any(r['status'] == 503 for r in tab['page']['resources']) or
                        any(r['http_status'] == '503' for r in tab['logs']['entries']))
        self.assertEqual([f.name for f in directory.iterdir()], ['diagnostic.json'])

    def test_normal_launch_uses_homepage_without_debugging_and_preserves_profile(self):
        local = Path(self.temp.name) / 'windows-user'
        profile = local / 'DiskverWBReturnQR' / 'BrowserProfile'
        profile.mkdir(parents=True)
        (profile / 'keep_auth.txt').write_text('KEEP_LOCAL_AUTH')
        result = self.run_ps(f"""
$env:OS = 'Windows_NT'; $env:LOCALAPPDATA = {ps_quote(local)}; $Normal = $true
function Find-TestBrowser {{ param($RequestedBrowser) 'chrome.exe' }}
function Get-CimInstance {{ param($ClassName, $Filter) }}
function Start-Process {{ param($FilePath, $ArgumentList) $script:launch = @{{file=$FilePath;arguments=$ArgumentList}} }}
Start-WbTest
'LAUNCH:' + ($script:launch | ConvertTo-Json -Compress)
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith('LAUNCH:')))
        args = launch['arguments']
        self.assertIn('https://www.wildberries.ru/', args)
        self.assertFalse(any('debug' in a for a in args))
        self.assertEqual((profile / 'keep_auth.txt').read_text(), 'KEEP_LOCAL_AUTH')

    def test_running_test_profile_blocks_accidental_debug_browser_reuse(self):
        local = Path(self.temp.name) / 'busy-windows-user'
        result = self.run_ps(f"""
$env:OS = 'Windows_NT'; $env:LOCALAPPDATA = {ps_quote(local)}; $Normal = $true
function Find-TestBrowser {{ param($RequestedBrowser) 'chrome.exe' }}
function Get-CimInstance {{ param($ClassName, $Filter)
  [pscustomobject]@{{CommandLine=('--user-data-dir=' + (Join-Path $env:LOCALAPPDATA 'DiskverWBReturnQR\\BrowserProfile'))}}
}}
function Start-Process {{ throw 'MUST_NOT_LAUNCH' }}
Start-WbTest
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Предыдущее тестовое окно', result.stderr)
        self.assertNotIn('MUST_NOT_LAUNCH', result.stderr)


if __name__ == '__main__':
    unittest.main()
