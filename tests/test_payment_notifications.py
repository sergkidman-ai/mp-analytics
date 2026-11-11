# поток: inv
import contextlib
import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import invoice_bot
invoice_bot.__path__ = [str(Path(__file__).resolve().parents[1] / 'invoice_bot')]
from invoice_bot import payment_send as sender, payment_autosend as autosend, tg_notify as delivery


class ManualNotificationsTests(unittest.TestCase):
    def setUp(self):
        self.draft = {'id': 501, 'org_inn': '7807355364', 'inn': 'supplier',
                      'kind': 'advance', 'amount': 100000, 'status': 'planned'}
        self.driver = Mock()
        self.driver.apply_enabled.return_value = True
        self.driver.send_one.return_value = {
            'ok': True, 'status': 'sent_prod', 'external_id': 'fixture-external-id', 'error': None,
            'payload': {'payeeName': 'Поставщик', 'purpose': 'Авансовый платёж'}}
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)

    def run_send(self, **kwargs):
        with patch.object(sender.pd, 'load_drafts', return_value=[self.draft]), \
             patch.object(sender, 'driver_for', return_value=self.driver), \
             patch.object(sender.db, 'get_conn', return_value=self.connection), \
             patch.object(sender.tg_notify, 'tg', return_value=True) as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            result = sender.send_draft(501, **kwargs)
        return result, notify

    def test_manual_production_creation_sends_one_private_notice(self):
        result, notify = self.run_send(dry_run=False)
        self.assertTrue(result['ok'])
        self.assertEqual(notify.call_count, 1)
        message = notify.call_args.args[0]
        self.assertIn('ЦИФРОВОЙ КВАДРАТ', message)
        self.assertIn('Поставщик', message)
        self.assertIn('100\u00a0000.00', message)
        self.assertIn('Подпишите', message)
        self.assertNotIn('fixture-external-id', message)
        self.driver.send_one.assert_called_once()

    def test_sber_manual_creation_names_its_organization(self):
        self.draft['org_inn'] = '7811803918'
        _, notify = self.run_send(dry_run=False)
        self.assertIn('ДИСКВЭР', notify.call_args.args[0])
        self.assertIn('Сбер', notify.call_args.args[0])

    def test_dry_run_does_not_notify(self):
        self.driver.send_one.return_value['status'] = 'dry_run'
        _, notify = self.run_send(dry_run=True)
        notify.assert_not_called()

    def test_sandbox_does_not_claim_a_new_production_payment(self):
        self.driver.send_one.return_value['status'] = 'sent_sandbox'
        _, notify = self.run_send(dry_run=False)
        notify.assert_not_called()

    def test_rejected_bank_request_does_not_notify_creation(self):
        self.driver.send_one.return_value.update(ok=False, status='error', error='fixture bank error')
        _, notify = self.run_send(dry_run=False)
        notify.assert_not_called()

    def test_already_sent_draft_is_not_announced_again(self):
        self.draft['status'] = 'sent_prod'
        result, notify = self.run_send(dry_run=False)
        self.assertFalse(result['ok'])
        notify.assert_not_called()
        self.driver.send_one.assert_not_called()

    def test_batch_can_suppress_individual_notice(self):
        _, notify = self.run_send(dry_run=False, notify=False)
        notify.assert_not_called()

    def test_telegram_failure_preserves_successful_bank_result(self):
        with patch.object(sender.pd, 'load_drafts', return_value=[self.draft]), \
             patch.object(sender, 'driver_for', return_value=self.driver), \
             patch.object(sender.db, 'get_conn', return_value=self.connection), \
             patch.object(sender.tg_notify, 'tg', side_effect=RuntimeError('fixture')), \
             contextlib.redirect_stdout(io.StringIO()):
            result = sender.send_draft(501, dry_run=False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'sent_prod')
        self.driver.send_one.assert_called_once()


class BatchNotificationsTests(unittest.TestCase):
    def run_batch(self, rows, status='sent_prod', notify=True):
        def load(org_inn, only_planned):
            return [row for row in rows if row['org_inn'] == org_inn]
        with patch.object(autosend.pdr, 'load_drafts', side_effect=load), \
             patch.object(autosend, '_names', return_value={'supplier': 'Поставщик'}), \
             patch.object(autosend, 'enabled', return_value=True), \
             patch.object(autosend.psend, 'send_draft', return_value={'ok': True, 'status': status}) as send, \
             patch.object(autosend, 'tg') as message, patch.object(autosend.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()):
            result = autosend.autosend(notify=notify, dry_run=(status == 'dry_run'))
        return result, send, message

    def rows(self):
        return [{'id': i, 'org_inn': org, 'inn': 'supplier', 'kind': 'advance',
                 'amount': 1000, 'created_at': '2026-11-11'}
                for i, org in enumerate(['7807355364', '7807355364', '7811803918'], 1)]

    def test_batch_sends_one_summary_per_org_without_individual_duplicates(self):
        result, send, message = self.run_batch(self.rows())
        self.assertEqual(result['sent'], 3)
        self.assertEqual(message.call_count, 2)
        self.assertEqual(send.call_count, 3)
        self.assertTrue(all(call.kwargs['notify'] is False for call in send.call_args_list))
        self.assertIn('ЦИФРОВОЙ КВАДРАТ', message.call_args_list[0].args[0])
        self.assertIn('ДИСКВЭР', message.call_args_list[1].args[0])

    def test_empty_or_dry_runs_do_not_message(self):
        for rows, status in [([], 'sent_prod'), (self.rows(), 'dry_run')]:
            with self.subTest(status=status, empty=not rows):
                _, _, message = self.run_batch(rows, status=status)
                message.assert_not_called()

    def test_explicit_suppression_keeps_all_batch_messages_off(self):
        _, send, message = self.run_batch(self.rows(), notify=False)
        message.assert_not_called()
        self.assertTrue(all(call.kwargs['notify'] is False for call in send.call_args_list))


class PrivateDeliveryTests(unittest.TestCase):
    def test_missing_payment_token_never_falls_back_to_general_bot(self):
        with patch.dict(os.environ, {'TG_PAY_BOT_TOKEN': '', 'TG_PAY_NOTIFY_ID': 'fixture-chat',
                                     'TG_BOT_TOKEN': 'general-bot-fixture'}), \
             patch.object(delivery, '_post') as post, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(delivery.tg('fixture message'))
        post.assert_not_called()

    def test_only_configured_private_payment_recipient_is_used(self):
        with patch.dict(os.environ, {'TG_PAY_BOT_TOKEN': 'payment-fixture', 'TG_PAY_NOTIFY_ID': 'natalia-fixture',
                                     'TG_NOTIFY_ID': 'unrelated-chat'}), \
             patch.object(delivery, '_post', return_value=(True, None)) as post:
            self.assertTrue(delivery.tg('fixture message'))
        self.assertEqual(post.call_args.args[1], 'natalia-fixture')
        self.assertIn('payment-fixture', post.call_args.args[0])
        post.assert_called_once()

    def test_transient_network_error_retries_delivery(self):
        response = Mock()
        response.read.return_value = b'{"ok":true}'
        with patch.object(delivery.urllib.request, 'urlopen', side_effect=[urllib.error.URLError('timeout'), response]) as request, \
             patch.object(delivery.time, 'sleep'):
            self.assertEqual(delivery._post('https://fixture.invalid', 'fixture-chat', 'test'), (True, None))
        self.assertEqual(request.call_count, 2)

    def test_permanent_telegram_error_is_reported_without_retries(self):
        error = urllib.error.HTTPError('https://fixture.invalid', 403, 'Forbidden', {}, io.BytesIO(b'{"description":"blocked"}'))
        with patch.object(delivery.urllib.request, 'urlopen', side_effect=error) as request, \
             patch.object(delivery.time, 'sleep'):
            ok, reason = delivery._post('https://fixture.invalid', 'fixture-chat', 'test')
        self.assertFalse(ok)
        self.assertIn('403', reason)
        request.assert_called_once()


if __name__ == '__main__':
    unittest.main()
