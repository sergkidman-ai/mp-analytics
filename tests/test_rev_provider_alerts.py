# поток: rev
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid
import requests

from core import db
from reports import batch_health as health, deepseek_search as search
from ops import rev_provider_watch as watch


class ProviderAlerts(unittest.TestCase):
    def setUp(self):
        health.setup()
        self.provider='test-alert-'+uuid.uuid4().hex
        self.sender=patch.object(health,'_send',return_value=987654321)
        self.send=self.sender.start()

    def tearDown(self):
        self.sender.stop()
        db.execute('DELETE FROM rev_provider_alert WHERE provider=%s',(self.provider,))
        db.execute('DELETE FROM rev_batch_health WHERE provider=%s',(self.provider,))

    def events(self):
        return db.query('SELECT * FROM rev_provider_alert WHERE provider=%s ORDER BY id',(self.provider,))

    def test_restart_does_not_repeat_problem_and_recovery_is_sent_once(self):
        error=SimpleNamespace(status_code=402,body={'message':'private secret should never appear'})
        health.record(error,self.provider)
        health._READY=False
        health.record(error,self.provider)
        self.assertEqual(self.send.call_count,1)
        self.assertNotIn('private secret',self.events()[0]['message'])
        health.clear(self.provider)
        health.clear(self.provider)
        self.assertEqual([e['kind'] for e in self.events()],['problem','recovery'])
        self.assertTrue(all(e['delivered_at'] and e['message_id'] for e in self.events()))

    def test_failed_telegram_delivery_is_durable_and_retried(self):
        self.send.return_value=None
        health.record(SimpleNamespace(status_code=401,body={}),self.provider)
        event=self.events()[0]
        self.assertIsNone(event['delivered_at'])
        self.assertEqual(event['last_error'],'TelegramNotConfirmed')
        self.send.return_value=987654322
        self.assertEqual(health.deliver_alerts(provider=self.provider),1)
        self.assertEqual(self.events()[0]['message_id'],987654322)
        self.assertEqual(health.deliver_alerts(provider=self.provider),0)

    def test_transient_escalates_without_false_topup_instruction(self):
        error=SimpleNamespace(status_code=429,body={'message':'rate limit'})
        for _ in range(3):health.record(error,self.provider)
        events=self.events()
        self.assertEqual([e['severity'] for e in events],['warning','critical'])
        self.assertTrue(all('оплат' not in e['message'] and 'Пополн' not in e['message'] for e in events))

    def test_low_balance_warning_does_not_block_work(self):
        health.observe(self.provider,'low_balance','Low balance',blocks_work=False,severity='warning')
        self.assertIsNone(health.blocked(self.provider))

    def test_balance_success_does_not_clear_search_permission_failure(self):
        health.record(SimpleNamespace(status_code=403,body={}),self.provider,source='request')
        self.assertFalse(health.clear(self.provider,source='balance'))
        self.assertTrue(health.blocked(self.provider))

    def test_concurrent_workers_deliver_one_problem(self):
        error=SimpleNamespace(status_code=402,body={})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:health.record(error,self.provider),range(2)))
        self.assertEqual(len(self.events()),1)
        self.assertEqual(self.send.call_count,1)

    def test_spend_cap_429_differs_from_frequency_429(self):
        cap=SimpleNamespace(status_code=429,body={'message':'monthly spend cap'})
        self.assertEqual(health.classify(cap)[0],'workspace_limit')
        self.assertTrue(health.rejected(SimpleNamespace(status_code=402)))


class DeepSeekRecovery(unittest.TestCase):
    def fetch(self, response=None, error=None):
        database=MagicMock()
        database.query.side_effect=[[{'day':'2026-11-11'}],[]]
        with patch.object(search,'db',database), patch.object(search.requests,'post',return_value=response,side_effect=error), \
             patch.object(search.batch_health,'record') as alert, \
             patch.object(search.recovery_budget,'setup'), \
             patch.object(search.recovery_budget,'reserve',return_value=True), \
             patch.dict('os.environ',{'DEEPSEEK_BASE_URL':'https://api.deepseek.com/anthropic'}):
            with self.assertRaises(RuntimeError):search._fetch('public question','query','hash','never_print_token')
        return database,alert

    def test_billing_rejection_releases_reservation_and_alerts(self):
        response=SimpleNamespace(ok=False,status_code=402,json=lambda:{'error':{'message':'Insufficient Balance'}})
        database,alert=self.fetch(response=response)
        self.assertIn('actual_usd=0',database.execute.call_args.args[0])
        self.assertEqual(alert.call_args.args[1],'deepseek')

    def test_unknown_timeout_retains_reservation_and_alerts(self):
        database,alert=self.fetch(error=requests.Timeout())
        database.execute.assert_not_called()
        alert.assert_called_once()

    def test_invalid_paid_response_retains_reservation_and_alerts(self):
        response=SimpleNamespace(ok=True,json=MagicMock(side_effect=ValueError()))
        database,alert=self.fetch(response=response)
        database.execute.assert_not_called()
        alert.assert_called_once()

    def test_balance_warning_continues_work_and_billing_recovery_is_scoped(self):
        response=SimpleNamespace(ok=True,json=lambda:{'is_available':True,'balance_infos':[{'currency':'USD','total_balance':'0.68'}]})
        with patch.object(watch,'batch_health') as state, patch.object(watch.requests,'get',return_value=response), \
             patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com/anthropic'}):
            result=watch.check_deepseek()
            self.assertTrue(result['available'])
            self.assertFalse(state.observe.call_args.kwargs['blocks_work'])
            state.clear.assert_called_once_with('deepseek',source='balance')

    def test_failed_balance_endpoint_does_not_overwrite_proven_search_failure(self):
        response=SimpleNamespace(ok=False,status_code=503)
        with patch.object(watch,'batch_health') as state, patch.object(watch.requests,'get',return_value=response), \
             patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com/anthropic'}):
            watch.check_deepseek()
            self.assertEqual(state.observe.call_args.args[0],'deepseek-balance')
            self.assertFalse(state.observe.call_args.kwargs['blocks_work'])
            state.record.assert_not_called()


if __name__=='__main__':unittest.main()
