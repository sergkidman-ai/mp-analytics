# поток: inv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from fastapi import HTTPException
from starlette.requests import Request
from ops import ms_supply_queue as queue
from web import ms_supply_receiver as receiver

ACCOUNT = str(uuid4())
RECEIPT = str(uuid4())
STAMP = '2026-11-03T12:00:00+00:00'


def event(receipt=RECEIPT, stamp=STAMP):
    return {'receipt_id': receipt, 'event_timestamp': stamp}


def payload():
    return {'auditContext': {'moment': '2026-11-03 15:00:00'}, 'events': [
        {'accountId': ACCOUNT, 'meta': {'type': 'supply',
         'href': 'https://api.moysklad.ru/api/remap/1.2/entity/supply/' + RECEIPT},
         'action': 'UPDATE', 'updatedFields': ['state']}]}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'queue/runtime.sqlite'

    def accept(self, events=None, request=None, now=100):
        return queue.accept(ACCOUNT, request or str(uuid4()), events or [event()], path=self.path, now=now)

    def test_delivery_duplicates_and_same_receipt_update_are_coalesced(self):
        request = str(uuid4())
        self.assertEqual(self.accept(request=request)['accepted'], 1)
        self.assertTrue(self.accept(request=request)['duplicate'])
        self.assertEqual(self.accept()['accepted'], 0)
        claimed = queue.claim(path=self.path, now=100)
        self.assertEqual(claimed['receipt_id'], RECEIPT)
        self.assertIsNone(queue.claim(path=self.path, now=100))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_claim_is_atomic_across_connections(self):
        self.accept()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: queue.claim(path=self.path, now=100), range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_lease_expiry_recovers_after_process_crash(self):
        self.accept()
        first = queue.claim(path=self.path, now=100, lease_seconds=10)
        self.assertIsNone(queue.claim(path=self.path, now=109))
        second = queue.claim(path=self.path, now=110)
        self.assertNotEqual(first['claim_token'], second['claim_token'])
        self.assertFalse(queue.finish(first, {'status': 'done'}, path=self.path, now=110))

    def test_new_event_while_running_is_not_lost_on_old_claim_completion(self):
        self.accept()
        first = queue.claim(path=self.path, now=100)
        self.accept([event(stamp='2026-11-03T12:01:00+00:00')], now=101)
        queue.finish(first, {'status': 'done'}, path=self.path, now=102)
        second = queue.claim(path=self.path, now=102)
        self.assertEqual(second['generation'], 2)

    def test_older_delayed_notification_does_not_reopen_completed_latest_work(self):
        self.accept([event(stamp='2026-11-03T12:01:00+00:00')])
        job = queue.claim(path=self.path, now=100)
        queue.finish(job, {'status': 'done'}, path=self.path, now=100)
        self.assertEqual(self.accept([event()], now=101)['accepted'], 0)
        self.assertIsNone(queue.claim(path=self.path, now=101))

    def test_delayed_stage_does_not_block_another_receipt(self):
        self.accept()
        first = queue.claim(path=self.path, now=100)
        queue.finish(first, {'status': 'continue', 'delay_seconds': 300}, path=self.path, now=100)
        other = str(uuid4())
        self.accept([event(other)], now=101)
        second = queue.claim(path=self.path, now=101)
        self.assertEqual(second['receipt_id'], other)
        queue.finish(second, {'status': 'done'}, path=self.path, now=101)
        self.assertIsNone(queue.claim(path=self.path, now=399))
        self.assertEqual(queue.claim(path=self.path, now=400)['receipt_id'], RECEIPT)

    def test_retry_backoff_and_manual_terminal_outcome(self):
        self.accept()
        job = queue.claim(path=self.path, now=100)
        queue.finish(job, {'status': 'retry'}, path=self.path, now=100)
        self.assertIsNone(queue.claim(path=self.path, now=109))
        job = queue.claim(path=self.path, now=110)
        queue.finish(job, {'status': 'manual'}, path=self.path, now=110)
        self.assertIsNone(queue.claim(path=self.path, now=10000))

    def test_not_before_is_respected_in_addition_to_delay(self):
        self.accept()
        job = queue.claim(path=self.path, now=100)
        queue.finish(job, {'status': 'continue', 'delay_seconds': 10,
                          'not_before': datetime.fromtimestamp(150, timezone.utc).isoformat()},
                     path=self.path, now=100)
        self.assertIsNone(queue.claim(path=self.path, now=149))
        self.assertIsNotNone(queue.claim(path=self.path, now=150))

    def test_async_worker_contract(self):
        self.accept(now=0)
        fulfill = Mock(return_value={'status': 'done'})
        result = queue.run_once(fulfill, path=self.path)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(fulfill.call_args.args[:2], (RECEIPT, STAMP))
        self.assertTrue(fulfill.call_args.kwargs['apply'])
        self.assertIsNone(queue.claim(path=self.path))

    def test_reconciliation_is_inactive_until_explicit_activation(self):
        get = Mock()
        self.assertEqual(queue.reconcile(path=self.path, get=get)['status'], 'inactive')
        get.assert_not_called()

    def test_reconciliation_has_a_lower_bound_and_never_calls_stock(self):
        activation = datetime.now(timezone.utc).isoformat()
        queue.activate_reconciliation(activation, path=self.path)
        response = Mock(status_code=200)
        response.json.return_value = {'meta': {'size': 0}, 'rows': []}
        get = Mock(return_value=response)
        self.assertEqual(queue.reconcile(path=self.path, get=get)['status'], 'done')
        self.assertTrue(get.call_args.args[0].endswith('/entity/supply'))
        self.assertIn('updated>=', get.call_args.kwargs['params']['filter'])
        self.assertIn(queue.ACCEPTED_STATE, get.call_args.kwargs['params']['filter'])

    def test_failed_reconciliation_does_not_advance_checkpoint(self):
        queue.activate_reconciliation(STAMP, path=self.path)
        get = Mock(return_value=Mock(status_code=503))
        with self.assertRaisesRegex(RuntimeError, 'reconciliation_http_503'):
            queue.reconcile(path=self.path, get=get)
        with queue.database(self.path) as db:
            checkpoint = db.execute("SELECT value FROM settings WHERE key='reconcile_checkpoint'").fetchone()[0]
        self.assertEqual(checkpoint, STAMP)

    def test_reconciliation_never_enqueues_history_before_activation(self):
        queue.activate_reconciliation(STAMP, path=self.path)
        response = Mock(status_code=200)
        response.json.return_value = {'meta': {'size': 2}, 'rows': [
            {'id': str(uuid4()), 'updated': '2026-11-03T11:59:59+00:00'},
            {'id': RECEIPT, 'updated': '2026-11-03T12:00:01+00:00'}]}
        result = queue.reconcile(path=self.path, get=Mock(return_value=response))
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(queue.claim(path=self.path)['receipt_id'], RECEIPT)


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'queue/runtime.sqlite'
        env = patch.dict(os.environ, {'MS_SUPPLY_WEBHOOK_SECRET': 'test-secret',
                                    'MS_SUPPLY_ACCOUNT_ID': ACCOUNT,
                                    'MS_SUPPLY_QUEUE_PATH': str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    async def request(self, value, secret='test-secret', query=None):
        data = json.dumps(value).encode()
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.disconnect'}
            sent = True
            return {'type': 'http.request', 'body': data, 'more_body': False}

        request = Request({'type': 'http', 'method': 'POST', 'path': '/hooks/ms-supply/redacted',
                           'headers': [], 'query_string': (query or 'requestId=' + str(uuid4())).encode()}, receive)
        return await receiver.receive(secret, request)

    async def test_ack_only_after_durable_accept_and_no_inline_worker(self):
        with patch.object(queue, 'run_once') as worker:
            response = await self.request(payload())
        self.assertEqual(response.status_code, 204)
        worker.assert_not_called()
        self.assertEqual(queue.claim(path=self.path)['receipt_id'], RECEIPT)

    async def test_invalid_token_is_not_saved(self):
        with self.assertRaises(HTTPException) as error:
            await self.request(payload(), secret='incorrect')
        self.assertEqual(error.exception.status_code, 404)
        self.assertFalse(self.path.exists())

    async def test_storage_failure_returns_503_for_upstream_retry(self):
        with patch.object(queue, 'accept', side_effect=OSError):
            with self.assertRaises(HTTPException) as error:
                await self.request(payload())
        self.assertEqual(error.exception.status_code, 503)

    async def test_non_state_changes_are_acknowledged_without_a_fulfillment_job(self):
        value = payload()
        value['events'][0]['updatedFields'] = ['description']
        self.assertEqual((await self.request(value)).status_code, 204)
        self.assertIsNone(queue.claim(path=self.path))

    async def test_account_entity_host_and_action_are_checked(self):
        for mutate in ('account', 'entity', 'host', 'action'):
            value = payload()
            item = value['events'][0]
            if mutate == 'account':
                item['accountId'] = str(uuid4())
            elif mutate == 'entity':
                item['meta']['type'] = 'demand'
            elif mutate == 'host':
                item['meta']['href'] = item['meta']['href'].replace('api.moysklad.ru', 'example.invalid')
            else:
                item['action'] = 'CREATE'
            with self.assertRaises(HTTPException):
                await self.request(value)

    async def test_body_limit_and_invalid_request_id(self):
        with patch.object(receiver, 'MAX_BODY', 8):
            with self.assertRaises(HTTPException) as error:
                await self.request(payload())
            self.assertEqual(error.exception.status_code, 413)
        with self.assertRaises(HTTPException) as error:
            await self.request(payload(), query='requestId=bad')
        self.assertEqual(error.exception.status_code, 400)

    async def test_public_health_reveals_only_readiness(self):
        self.assertEqual(receiver.health(), {'status': 'ready'})


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.secret = 'test-only-placeholder-secret-' + 'x' * 32
        self.url = 'https://89-207-255-54.sslip.io/hooks/ms-supply/' + self.secret
        env = patch.dict(os.environ, {'MS_SUPPLY_WEBHOOK_SECRET': self.secret,
                                    'MS_SUPPLY_PUBLIC_BASE_URL': 'https://89-207-255-54.sslip.io'})
        env.start()
        self.addCleanup(env.stop)
        runtime = patch.object(queue, 'RUNTIME', self.folder)
        runtime.start()
        self.addCleanup(runtime.stop)
        token = patch('core.ms_api.TOKEN', 'test-token')
        token.start()
        self.addCleanup(token.stop)

    def mocks(self):
        listing = Mock(status_code=200)
        listing.json.return_value = {'meta': {'size': 1}, 'rows': [
            {'id': 'unrelated', 'url': 'https://example.invalid/old',
             'entityType': 'supply', 'action': 'UPDATE', 'enabled': True}]}
        created = Mock(status_code=200)
        created.json.return_value = {'id': 'new-id', 'url': self.url, 'entityType': 'supply',
                                    'action': 'UPDATE', 'enabled': True}
        return listing, created

    def test_register_only_adds_our_subscription_and_redacts_audit_files(self):
        listing, created = self.mocks()
        with patch('requests.get', side_effect=[Mock(status_code=200), listing]), \
                patch('requests.post', side_effect=[Mock(status_code=400), created]) as post, \
                patch.object(queue, 'activate_reconciliation') as activate:
            result = queue.register_webhook()
        self.assertEqual(result['id'], 'new-id')
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args.args[0].endswith('/entity/webhook'))
        self.assertEqual(post.call_args.kwargs['json']['diffType'], 'FIELDS')
        self.assertEqual(post.call_args.kwargs['json']['entityType'], 'supply')
        self.assertTrue(activate.called)
        for file in (self.folder / 'webhook_registration').iterdir():
            self.assertNotIn(self.secret, file.read_text())

    def test_uncertain_creation_is_not_blindly_repeated(self):
        listing, _ = self.mocks()
        with patch('requests.get', side_effect=[Mock(status_code=200), listing]), \
                patch('requests.post', side_effect=[Mock(status_code=400), requests.Timeout]):
            with self.assertRaisesRegex(RuntimeError, 'no_blind_retry'):
                queue.register_webhook()
        with patch('requests.get', side_effect=[Mock(status_code=200), listing]), \
                patch('requests.post', return_value=Mock(status_code=400)) as post:
            with self.assertRaisesRegex(RuntimeError, 'prior_webhook_intent'):
                queue.register_webhook()
            self.assertEqual(post.call_count, 1)  # Public invalid-payload probe only.


if __name__ == '__main__':
    unittest.main()
