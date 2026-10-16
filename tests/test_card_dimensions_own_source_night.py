# поток: card
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from tools import card_dimensions_own_source_night as m


class OwnSourceNightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.patch(m, 'OUT', self.out)
        self.row = {'id': 'p', 'article': 'art', 'external_code': '1234', 'supplier_id': 's',
                    'name': 'Тестовый картридж',
                    'dimensions': [{'id': str(i), 'value': str(i + 2)} for i in range(3)]}
        self.live = {'id': 'p', 'article': 'art', 'externalCode': '1234', 'name': self.row['name'],
                     'supplier': {'meta': {'href': 'https://example.test/entity/counterparty/s'}},
                     'attributes': [{'id': 'other', 'value': 'keep'}], 'barcodes': [{'ean8': '12345678'}]}

    def patch(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    def allow_time(self):
        self.patch(m, 'START', datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.patch(m, 'END', datetime(2100, 1, 1, tzinfo=timezone.utc))
        self.patch(m.ms_api, 'TOKEN', 'dummy')

    def test_only_empty_dimensions_change(self):
        original = copy.deepcopy(self.live)
        body = m.patch(self.live, self.row, set())
        self.assertEqual(set(body), {'id', 'attributes'})
        self.assertEqual(body['attributes'][0], original['attributes'][0])
        self.assertEqual(body['attributes'][1:], self.row['dimensions'])
        self.assertEqual(self.live, original)

    def test_partial_fill_is_skipped(self):
        self.live['attributes'].append(self.row['dimensions'][0])
        with self.assertRaisesRegex(RuntimeError, 'NO_LONGER_ALL_EMPTY'):
            m.patch(self.live, self.row, set())

    def test_identical_all_filled_skips(self):
        self.live['attributes'].extend(self.row['dimensions'])
        self.assertIsNone(m.patch(self.live, self.row, set()))

    def test_identity_drift(self):
        for field, value in [('article', 'different'), ('externalCode', 'other'), ('archived', True), ('name', 'Changed')]:
            with self.subTest(field=field):
                live = copy.deepcopy(self.live)
                live[field] = value
                with self.assertRaises(RuntimeError):
                    m.patch(live, self.row, set())

    def test_blacklist(self):
        with self.assertRaisesRegex(RuntimeError, 'BLACKLIST'):
            m.patch(self.live, self.row, {m.blacklist.norm(self.row['article'])})

    def test_readback_checks_other_fields(self):
        body = m.patch(self.live, self.row, set())
        after = {**self.live, 'attributes': body['attributes']}
        m.verify(after, body, self.live)
        after['barcodes'] = []
        with self.assertRaisesRegex(RuntimeError, 'barcodes'):
            m.verify(after, body, self.live)

    def test_request_header_without_global_subscription_write(self):
        self.allow_time()
        post = self.patch(m.requests, 'post', Mock(return_value=Mock(status_code=200, json=lambda: [{'id': 'p'}])))
        put = self.patch(m.requests, 'put', Mock(side_effect=AssertionError('Subscription mutation forbidden')))
        prefix = 'https://example.test/hooks/private-secret'
        m.send([{'id': 'p', 'attributes': []}], prefix)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['headers'][m.HEADER], prefix)
        self.assertNotIn(prefix, (self.out / 'journal.jsonl').read_text())
        put.assert_not_called()

    def test_uncertain_write_has_journal_and_no_retry(self):
        self.allow_time()
        post = self.patch(m.requests, 'post', Mock(side_effect=m.requests.Timeout()))
        with self.assertRaisesRegex(RuntimeError, 'no retry'):
            m.send([{'id': 'p'}], 'https://example.test/hook')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads((self.out / 'journal.jsonl').read_text())['ids'], ['p'])

    def test_daytime_no_post(self):
        self.patch(m, 'START', datetime(2100, 1, 1, tzinfo=timezone.utc))
        post = self.patch(m.requests, 'post', Mock())
        with self.assertRaisesRegex(RuntimeError, 'Outside'):
            m.send([{'id': 'p'}], 'https://example.test/hook')
        post.assert_not_called()
        self.assertFalse((self.out / 'journal.jsonl').exists())

    def test_new_consumer_blocks(self):
        url = 'https://example.test/hook'
        self.patch(m, 'HOOK_SHA', hashlib.sha256(url.encode()).hexdigest())
        hook = {'id': m.HOOK_ID, 'url': url, 'entityType': 'product', 'action': 'UPDATE', 'enabled': True}
        self.patch(m.ms_api, 'get', lambda *args: {'meta': {'size': 1}, 'rows': [hook]})
        self.assertEqual(m.checked_prefix(), url)
        self.patch(m.ms_api, 'get', lambda *args: {'meta': {'size': 2}, 'rows': [hook, dict(hook, id='other')]})
        with self.assertRaisesRegex(RuntimeError, 'consumers'):
            m.checked_prefix()

    def test_source_tamper_blocks(self):
        source = self.out / 'supplier.xlsx'
        source.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'source changed'):
            m.source_gate([], {str(source): 'old-hash'})

    def test_existing_journal_blocks_apply(self):
        (self.out / 'authorization.json').write_text(json.dumps({'authorized': True, 'notification_mode': m.MODE}))
        (self.out / 'summary.json').write_text('{}')
        (self.out / 'journal.jsonl').write_text('uncertain write')
        with self.assertRaisesRegex(RuntimeError, 'Existing journal'):
            m.apply()


if __name__ == '__main__':
    unittest.main()
