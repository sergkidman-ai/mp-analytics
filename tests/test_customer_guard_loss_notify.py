# поток: fin
"""Check the actual short alert contract and independent extra-recipient routing."""
import unittest
from unittest.mock import Mock, patch

from customer_guard import notifications as primary, extra_loss_notify as extra


def loss_row():
    return {'id': 10, 'kind': 'non_positive_profit', 'platform': 'ozon', 'account': 'oz_acc1',
            'posting_number': '31249608-0552-1', 'ms_status': 'Неукомплектован',
            'result': {'price': '2168.00', 'cost': '1169.80', 'cost_kind': 'fifo_estimate',
                       'estimated_net': '-285.91', 'rule_version': 'current', 'floor': '0.00',
                       'positions': [{'code': '7200oem', 'external_code': '7200', 'quantity': 1.0}]}}


class LossNotifyTests(unittest.TestCase):
    def test_identical_short_text_for_both_recipients(self):
        self.assertEqual(primary.message(loss_row()),
            '🚨 СРОЧНО: УБЫТОЧНЫЙ ЗАКАЗ\n'
            'Ozon · Цифровой · 31249608-0552-1\n'
            'МС: Неукомплектован\n'
            'Код МС: 7200oem; внешний код: 7200 × 1.0\n'
            'Цена: 2168.00 ₽; себестоимость: 1169.80 ₽ (предварительная оценка).\n'
            'Расчётная чистая прибыль: -285.91 ₽.')

    def test_zero_profit_has_siren_too(self):
        row = loss_row()
        row['result']['estimated_net'] = '0.00'
        self.assertTrue(primary.message(row).startswith('🚨 ВАЖНО: ЗАКАЗ БЕЗ ПРИБЫЛИ\n'))

    def test_extra_delivery_has_own_receipt_and_no_second_send(self):
        row = loss_row()
        response = Mock(status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 44}})
        with patch.object(extra, 'settings', return_value=('780828338', 'blackdragon2233', 'since')), \
             patch.object(extra, '_candidate_exists', side_effect=[True, False]), \
             patch.object(extra, 'checked_transport', return_value='https://unused.invalid'), \
             patch.object(extra, '_claim', side_effect=[row, None]), \
             patch.object(extra, 'expense_basis', return_value={'version':'current'}), \
             patch.object(extra.db, 'query', return_value=[{'?column?':1}]), \
             patch.object(extra, '_complete', return_value='sent') as complete, \
             patch.object(extra.requests, 'post', return_value=response) as post:
            self.assertEqual(extra.send_pending(limit=2)['sent'], 1)
            self.assertEqual(extra.send_pending(limit=2)['sent'], 0)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs['json']['chat_id'], '780828338')
            self.assertEqual(post.call_args.kwargs['json']['text'], primary.message(row))
            complete.assert_called_once_with(10, '780828338', 'sent', 44)

    def test_no_config_means_no_lookup_or_send(self):
        with patch.object(extra, 'settings', return_value=None), \
             patch.object(extra, '_candidate_exists') as candidates:
            self.assertEqual(extra.send_pending()['sent'], 0)
            candidates.assert_not_called()


if __name__ == '__main__':
    unittest.main()
