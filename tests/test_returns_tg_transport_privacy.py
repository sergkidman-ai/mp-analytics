# поток: ret
import traceback
import unittest
from unittest.mock import Mock, patch

import requests
from returns_bot import tg


class TransportPrivacyTests(unittest.TestCase):
    def test_exhausted_retries_hide_token_in_formatted_traceback(self):
        secret = '99999999:TEST_SECRET_NOT_FOR_LOGS_0123456789'
        error = requests.ConnectionError('failed /bot' + secret + '/getUpdates')
        with patch.object(tg, 'token', return_value=secret), \
             patch.object(tg.requests, 'post', side_effect=error) as post, \
             patch.object(tg.time, 'sleep'):
            try:
                tg._api('getUpdates', {}, tries=2)
            except RuntimeError as exc:
                formatted = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self.assertNotIn(secret, formatted)
                self.assertIn('transport failure (ConnectionError)', formatted)
            else:
                self.fail('Expected a sanitized transport error')
            self.assertEqual(post.call_count, 2)

    def test_get_updates_preserves_offset_and_poll_retry_limit(self):
        with patch.object(tg, '_api', return_value=[]) as api:
            self.assertEqual(tg.get_updates(123, timeout=25), [])
            api.assert_called_once_with('getUpdates', {'timeout':25, 'offset':123}, tries=2)

    def test_successful_response_is_unchanged(self):
        response = Mock(status_code=200)
        response.json.return_value = {'ok':True, 'result':[{'update_id':123}]}
        with patch.object(tg, 'token', return_value='test'), patch.object(tg.requests, 'post', return_value=response):
            self.assertEqual(tg._api('getUpdates', {}), [{'update_id':123}])


if __name__ == '__main__':
    unittest.main()
