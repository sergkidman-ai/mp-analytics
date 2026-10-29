# поток: inv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import invoice_bot
invoice_bot.__path__ = [str(Path(__file__).resolve().parents[1] / 'invoice_bot')]
from invoice_bot import payment_integrity_alert as alert
from invoice_bot import payment_integrity as integrity
from invoice_bot import po_payment_watch as watch


class PaymentIntegrityAlertTests(unittest.TestCase):
    def test_matching_reason_sent_once_per_day_and_retried_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / 'alerts.json'
            reason = 'КТ-000137: для организации требуется склад «Дисквер»'
            day = datetime(2026, 10, 29, 9, tzinfo=ZoneInfo('Europe/Moscow'))
            with patch.object(alert, '_send', side_effect=[False, True, True]) as send:
                self.assertFalse(alert.notify_assignment('7811803918', 'Поставщик', reason, ledger=ledger, now=day))
                self.assertFalse(ledger.exists())
                self.assertTrue(alert.notify_assignment('7811803918', 'Поставщик', reason, ledger=ledger, now=day))
                self.assertFalse(alert.notify_assignment('7811803918', 'Поставщик', reason, ledger=ledger, now=day))
                self.assertTrue(alert.notify_assignment('7811803918', 'Поставщик', reason, ledger=ledger, now=day.replace(day=30)))
                self.assertEqual(send.call_count, 3)
                self.assertIn('КТ-000137', send.call_args.args[0])
                self.assertEqual(len(json.loads(ledger.read_text())), 2)

    def test_unrelated_payment_error_sends_nothing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(alert, '_send') as send:
            self.assertFalse(alert.notify_assignment('7811803918', 'Поставщик',
                'Сумма черновика превышает текущую задолженность', ledger=Path(folder) / 'alerts.json'))
            send.assert_not_called()

    def test_watcher_alerts_on_assignment_stop_only(self):
        terms = [{'inn': 'supplier-inn', 'name': 'Поставщик', 'method': 'deferred',
                  'deferral_days': 6, 'payment_cap': None, 'ms_agent_id': None}]
        mismatch = integrity.PaymentIntegrityError('КТ-000137: склад приёмки отличается от заказа/организации')
        with patch.object(watch.db, 'query', return_value=terms), \
             patch.object(watch, 'process_deferred', side_effect=[mismatch, RuntimeError('network')]), \
             patch('invoice_bot.payment_integrity_alert.notify_assignment') as notify:
            watch.run()
            notify.assert_called_once_with(watch.BUYER_INN, 'Поставщик', str(mismatch))
            watch.run()
            notify.assert_called_once()


if __name__ == '__main__':
    unittest.main()
