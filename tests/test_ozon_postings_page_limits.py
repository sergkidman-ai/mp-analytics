# поток: fin
"""FBS v4 rejects the legacy default 1000; exercise the normal collector path."""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from collectors import ozon_postings as api


class PostingPageLimits(unittest.TestCase):
    def setUp(self):
        self.contexts = contextlib.ExitStack()
        self.addCleanup(self.contexts.close)
        response = Mock(status_code=200)
        response.json.return_value = {'postings': [], 'has_next': False, 'cursor': ''}
        self.http = self.contexts.enter_context(patch.object(api.requests, 'post', return_value=response))
        self.contexts.enter_context(patch.object(api, '_headers', return_value={}))
        self.contexts.enter_context(patch.object(api, 'account_lock',
            side_effect=lambda *a, **kw: contextlib.nullcontext(SimpleNamespace(check=lambda: None))))
        self.contexts.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_normal_financial_collector_uses_fbs_supported_default(self):
        with patch.object(api.db, 'upsert', return_value=0):
            api.main('2026-10-01', '2026-10-20', 'oz_acc1')
        self.assertEqual(len(self.http.call_args_list), 2)
        fbs_call = self.http.call_args_list[0]
        self.assertEqual(fbs_call.args[0], api.FBS_URL)
        self.assertEqual(fbs_call.kwargs['json']['limit'], 100)

    def test_fbs_rejects_oversized_page_before_http(self):
        for limit in (101, 1000):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                api.fetch_postings('oz_acc1', 'fbs', '2026-10-01', '2026-10-20', limit=limit)
        self.http.assert_not_called()

    def test_fbo_still_accepts_explicit_1000(self):
        api.fetch_postings('oz_acc1', 'fbo', '2026-10-01', '2026-10-20', limit=1000)
        self.assertEqual(self.http.call_args.kwargs['json']['limit'], 1000)


if __name__ == '__main__':
    unittest.main()
