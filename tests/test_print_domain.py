# поток: print
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from print_service.domain import (OrderPrintFiles, PrintBlocked, freeze_batch,
    guard_sample_export, ozon_warehouse_color, planned_for_today, selected_order)
from print_service.warehouse_colors import load_config, warehouse_color


class PrintDomainTests(unittest.TestCase):
    def test_express_uses_available_actions_and_neutral_delivery_marker(self):
        from print_service.domain import ozon_label_available
        for status in ('delivering','awaiting_registration','awaiting_deliver'):
            self.assertTrue(ozon_label_available(dict(status=status,is_express=True,available_actions=['label_download_small'])))
        self.assertFalse(ozon_label_available(dict(status='cancelled',available_actions=['label_download'])))
        self.assertFalse(ozon_label_available(dict(status='awaiting_packaging',available_actions=['cancel'])))
        self.assertFalse(ozon_label_available(dict(status='awaiting_deliver',is_express=True)))
        self.assertTrue(ozon_label_available(dict(status='awaiting_deliver')))
        self.assertEqual(warehouse_color('oz_acc1','1020002587140000',is_express=True)['short'],'ЭКС')
        self.assertIsNone(warehouse_color('oz_acc1','1020002587140000'))

    def test_confirmed_custom_warehouse_colors(self):
        config = load_config()
        self.assertEqual(sum(len(rows) for rows in config.values()), 10)
        self.assertEqual(warehouse_color('oz_acc1', 1020001848205000)['short'], 'Флт')
        self.assertEqual(warehouse_color('oz_acc2', '1020005005745590')['short'], 'Сер')
        self.assertEqual(warehouse_color('oz_acc2', '1020005005745680')['short'], 'Роз')
        # A similarly named old warehouse must not inherit the ZVZ color.
        self.assertIsNone(warehouse_color('oz_acc2', '1020005000417931'))
        self.assertIsNone(warehouse_color('oz_acc1', '1020005005745590'))

    def test_moscow_midnight(self):
        now = datetime(2026, 11, 2, 21, 5, tzinfo=timezone.utc)
        self.assertTrue(planned_for_today('2026-11-03 00:00:00', now))
        self.assertFalse(planned_for_today('2026-11-02 23:59:00', now))
        self.assertFalse(planned_for_today(None, now))

    def test_selection_excludes_nonexistent_yandex_account(self):
        order = {'state': {'name': 'Собирается'}, 'deliveryPlannedMoment': '2026-11-03 12:00:00'}
        now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
        self.assertTrue(selected_order(order, 'ya_acc1', now))
        self.assertFalse(selected_order(order, 'ya_acc2', now))

    def test_color_requires_explicit_account_warehouse_mapping(self):
        mapping = {'oz_acc1': {'123': 'black'}, 'oz_acc2': {'123': 'orange'}}
        self.assertEqual(ozon_warehouse_color(mapping, 'oz_acc1', '123')['short'], 'Чрн')
        self.assertEqual(ozon_warehouse_color(mapping, 'oz_acc2', '123')['short'], 'Орж')
        self.assertIsNone(ozon_warehouse_color(mapping, 'oz_acc1', 'unknown'))

    def test_sample_export_requires_printed_and_bidirectional_unique_link(self):
        base = 'https://api.moysklad.ru/api/remap/1.2/entity/'
        demand = {'id': 'd1', 'printed': True,
                  'meta': {'type': 'demand', 'href': base + 'demand/d1'},
                  'customerOrder': {'meta': {'href': base + 'customerorder/o1'}}}
        order = {'id': 'o1', 'demands': [{'meta': dict(demand['meta'])}]}
        guard_sample_export(order, demand, linked_order_id='o1')
        for printed in (False, None, 1, 'true'):
            with self.assertRaises(PrintBlocked):
                guard_sample_export(order, dict(demand, printed=printed), linked_order_id='o1')
        with self.assertRaises(PrintBlocked):
            guard_sample_export(dict(order, demands=order['demands'] * 2), demand, linked_order_id='o1')
        with self.assertRaises(PrintBlocked):
            guard_sample_export(order, dict(demand, customerOrder={}), linked_order_id='o1')

    def test_identical_sequence_with_multipage_receipt_and_multiple_boxes(self):
        first = OrderPrintFiles('oz_acc1', '0164911610-0107-3', 'o1', 'd1', ('r1', 'r2'), ('b1', 'b2'))
        second = OrderPrintFiles('oz_acc1', '0164911610-0107-4', 'o2', 'd2', ('r3',), ('b3',))
        manifest = freeze_batch((first, second), shipment_date=date(2026, 11, 3))
        self.assertEqual([r['external_id'] for r in manifest['orders']], [first.external_id, second.external_id])
        self.assertEqual(manifest['orders'][0]['labels'], ['b1', 'b2'])
        reversed_batch = freeze_batch((second, first), shipment_date=date(2026, 11, 3))
        self.assertNotEqual(manifest['sequence_id'], reversed_batch['sequence_id'])
        for other in (first, replace(second, ms_demand_id='d1')):
            with self.assertRaises(PrintBlocked):
                freeze_batch((first, other), shipment_date=date(2026, 11, 3))
        with self.assertRaises(PrintBlocked):
            replace(first, label_files=())


if __name__ == '__main__':
    unittest.main()
