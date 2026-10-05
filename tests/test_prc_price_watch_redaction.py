# поток: prc
"""Токен Telegram не попадает в диагностику, файлы журнала и текст уведомлений."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from ops import prc_price_watch as watcher


TOKEN = '123456789:dummy_telegram_secret_for_test_only'
OLD_TOKEN = '987654321:previous_dummy_secret_for_test_only'


class TelegramRedactionTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = patch.object(watcher, 'TG_TOKEN', TOKEN)
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def test_network_error_keeps_reason_without_token(self):
        error = requests.ConnectionError(
            f'Failed to connect /bot{TOKEN}/sendMessage: Network is unreachable')
        with patch.object(watcher.requests, 'post', side_effect=error):
            result = watcher.tg('test', chats=['42'])
        self.assertNotIn(TOKEN, result)
        self.assertIn('ConnectionError', result)
        self.assertIn('Network is unreachable', result)

    def test_response_and_outgoing_text_are_redacted_before_truncation(self):
        response = Mock(ok=False, status_code=500, text='x' * 110 + TOKEN)
        with patch.object(watcher.requests, 'post', return_value=response) as post:
            result = watcher.tg('x' * (watcher.TG_LIMIT - 5) + TOKEN, chats=['42'])
        self.assertNotIn(TOKEN[:10], result)
        self.assertNotIn(TOKEN[:5], post.call_args.kwargs['data']['text'])

    def test_old_and_encoded_tokens_are_redacted_without_current_token(self):
        with patch.object(watcher, 'TG_TOKEN', ''):
            for token in [OLD_TOKEN, OLD_TOKEN.replace(':', '%3A')]:
                result = watcher.redact_telegram_token(f'https://api.telegram.org/bot{token}/getMe')
                self.assertNotIn('previous_dummy_secret', result)
                self.assertIn('/getMe', result)

    def test_file_log_is_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watch.log'
            watcher.log(path, f'URL /bot{OLD_TOKEN}/sendMessage; {TOKEN}')
            result = path.read_text()
        self.assertNotIn(TOKEN, result)
        self.assertNotIn(OLD_TOKEN, result)
        self.assertIn('[REDACTED]', result)

    def test_crash_trace_and_returned_error_are_redacted(self):
        profile = Mock(key='test', unprocessed=True)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(watcher, 'LOG_DIR', Path(folder)), \
                    patch.object(watcher.unprocessed, 'main',
                                 side_effect=requests.ConnectionError(f'/bot{TOKEN}/sendMessage')):
                result, code = watcher.run_load(profile, True)
                crash = (Path(folder) / 'prc_price_watch_test_crash.log').read_text()
        self.assertEqual(code, 3)
        self.assertNotIn(TOKEN, result)
        self.assertNotIn(TOKEN, crash)
        self.assertIn('ConnectionError', crash)


if __name__ == '__main__':
    unittest.main()
