# поток: inv
from unittest import TestCase

from ops.ms_market_status_sync import target, interpret_wb, interpret_yandex


class StatusTargetTests(TestCase):
    def test_terminal_states_from_both_source_states(self):
        for source in ('Собирается', 'В пути'):
            self.assertEqual(target(source, 'delivered'), 'Доставлен')
            self.assertEqual(target(source, 'cancelled'), 'Отменен - сроки')

    def test_no_regression_or_unproven_change(self):
        self.assertEqual(target('Собирается', 'transit'), 'В пути')
        self.assertIsNone(target('В пути', 'transit'))
        self.assertIsNone(target('В пути', 'other'))
        self.assertIsNone(target('Доставлен', 'cancelled'))

    def test_wb_complete_waiting_is_not_handoff(self):
        self.assertEqual(interpret_wb({'supplierStatus': 'complete', 'wbStatus': 'waiting'})[0], 'other')
        self.assertEqual(interpret_wb({'supplierStatus': 'complete', 'wbStatus': 'sent_to_carrier'})[0], 'transit')
        self.assertEqual(interpret_wb({'supplierStatus': 'complete', 'wbStatus': 'sold'})[0], 'delivered')

    def test_yandex_only_real_fbs(self):
        row = {'programType': 'FBS', 'fake': False, 'status': 'DELIVERY'}
        self.assertEqual(interpret_yandex(row)[0], 'transit')
        with self.assertRaises(ValueError):
            interpret_yandex({**row, 'programType': 'DBS'})
