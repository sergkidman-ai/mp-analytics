# поток: inv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from ops.fbs_ms_post_import_status import all_available, desired, planned_day, provenance, pos_signature, proof_signature

MSK = ZoneInfo('Europe/Moscow')


class PostImportStatusTests(unittest.TestCase):
    def test_calendar_avoids_november_fourth_holiday(self):
        now = datetime(2026, 11, 2, 16, tzinfo=MSK)
        self.assertEqual(planned_day('Подтвержден', now), '2026-11-03')
        self.assertEqual(planned_day('Неукомплектован', now), '2026-11-05')
        self.assertEqual(planned_day('Подтвержден', datetime(2026, 11, 3, 12, tzinfo=MSK)), '2026-11-05')
        self.assertEqual(planned_day('Подтвержден', datetime(2026, 11, 5, 15, tzinfo=MSK)), '2026-11-05')
        self.assertEqual(planned_day('Подтвержден', datetime(2026, 11, 5, 16, tzinfo=MSK)), '2026-11-06')

    def test_own_reserve_counts_but_physical_stock_is_required(self):
        position = {'assortment': {'id': 'p'}, 'quantity': 2, 'reserve': 2}
        order = {'positions': {'rows': [position]}}
        self.assertEqual(all_available(order, {'p': {'stock': 2, 'reserve': 2}}, defaultdict(Decimal))[0], True)
        self.assertEqual(all_available(order, {'p': {'stock': 1, 'reserve': 2}}, defaultdict(Decimal))[0], False)
        position['reserve'] = 0
        self.assertEqual(all_available(order, {'p': {'stock': 2, 'reserve': 1}}, defaultdict(Decimal))[0], False)

    def test_shortage_requires_real_price_choice(self):
        priced = {'selected': [{'available': False, 'purchase_kopeks': 43000,
                                'manual_fallback': False}]}
        fallback = {'selected': [{'available': False, 'purchase_kopeks': None,
                                  'manual_fallback': True}]}
        self.assertEqual(provenance(priced), (False, True))
        self.assertEqual(provenance(fallback), (True, False))

    def test_already_processed_order_cannot_be_changed(self):
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/product/p'
        order = {'name': '123', 'sum': 100, 'state': {'name': 'Неукомплектован'},
                 'store': {'meta': {'href': 'https://api.moysklad.ru/api/remap/1.2/entity/store/s'}},
                 'positions': {'rows': [{'assortment': {'meta': {'href': href}},
                                         'quantity': 1, 'reserve': 0}]}}
        proof = {'total_kopeks': 100, 'selected': [{'available': False, 'purchase_kopeks': 50}],
                 'payload': {'store': order['store'], 'positions': [
                     {'assortment': {'meta': {'href': href}}, 'quantity': 1}]}}
        target, day, evidence = desired(order, proof, {'p': {'stock': 0, 'reserve': 0}},
                                        defaultdict(Decimal), datetime(2026, 11, 2, 16, tzinfo=MSK))
        self.assertIsNone(target)
        self.assertIsNone(day)
        self.assertEqual(evidence['reason'], 'already_processed_or_payment_pending')

    def test_split_set_with_all_components_on_order_store_is_confirmed(self):
        root = 'https://api.moysklad.ru/api/remap/1.2/entity/'
        positions = [{'assortment': {'meta': {'href': root + 'product/' + code}},
                      'quantity': 1, 'reserve': 1} for code in ('a', 'b')]
        order = {'name': 'kit-order', 'sum': 90000, 'state': {'name': 'Ожидает подтверждения'},
                 'store': {'meta': {'href': root + 'store/s'}},
                 'positions': {'rows': positions}, 'demands': []}
        proof = {'total_kopeks': 90000,
                 'selected': [{'available': True, 'manual_parent': False, 'card_id': code}
                              for code in ('a', 'b')],
                 'payload': {'store': order['store'], 'positions': positions}}
        stock = {code: {'stock': 1, 'reserve': 1} for code in ('a', 'b')}
        target, day, _ = desired(order, proof, stock, defaultdict(Decimal),
                                 datetime(2026, 11, 2, 16, tzinfo=MSK))
        self.assertEqual((target, day), ('Подтвержден', '2026-11-03'))

    def test_merged_identical_component_lines_preserve_quantity_check(self):
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/product/a'
        proof = {'payload': {'positions': [
            {'assortment': {'meta': {'href': href}}, 'quantity': 1},
            {'assortment': {'meta': {'href': href}}, 'quantity': 1}]}}
        merged = [{'assortment': {'meta': {'href': href}}, 'quantity': 2}]
        self.assertEqual(pos_signature(merged), proof_signature(proof))


if __name__ == '__main__':
    unittest.main()
