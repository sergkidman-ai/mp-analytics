# поток: inv
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from ops.fbs_ms_order_payload import Line, build_payload, line_total


class PayloadTests(unittest.TestCase):
    def test_ozon_original_price_and_discount_preserve_unrounded_total(self):
        line = Line('product-id', Decimal(1), 424300, Decimal('10.06'))
        self.assertEqual(line_total(line), 381615)
        order = build_payload(platform='ozon', account='acc1', external_id='27409962-0191-1',
                              lines=[line], expected_kopeks=381615, available=True,
                              now=datetime(2026, 11, 2, 12, 59, tzinfo=timezone.utc))
        self.assertEqual(order['positions'][0]['price'], 424300)
        self.assertEqual(order['positions'][0]['discount'], 10.06)
        self.assertEqual(order['positions'][0]['vat'], 5)
        self.assertTrue(order['vatIncluded'])
        self.assertEqual(order['deliveryPlannedMoment'], '2026-11-02 00:00:00')
        self.assertIn('/customerorder/metadata/states/', order['state']['meta']['href'])

    def test_after_16_moscow_goes_next_day(self):
        order = build_payload(platform='yandex', account='acc1', external_id='63899033921',
                              lines=[Line('p', Decimal(1), 111600)], expected_kopeks=111600,
                              available=True, now=datetime(2026, 11, 2, 13, tzinfo=timezone.utc))
        self.assertEqual(order['deliveryPlannedMoment'], '2026-11-03 00:00:00')

    def test_unavailable_has_waiting_state_no_date_or_reserve_instruction(self):
        order = build_payload(platform='wb', account='acc1', external_id='6176116763',
                              lines=[Line('p', Decimal(1), 349100)], expected_kopeks=349100,
                              available=False, now=datetime(2026, 11, 2, 13, tzinfo=timezone.utc))
        self.assertNotIn('deliveryPlannedMoment', order)
        self.assertNotIn('reserve', order['positions'][0])
        self.assertIn('7e4cf8b8', order['state']['meta']['href'])

    def test_unpaid_yandex_has_payment_state_without_planned_date(self):
        order = build_payload(platform='yandex', account='acc1', external_id='63898586689',
                              lines=[Line('p', Decimal(1), 220700)], expected_kopeks=220700,
                              available=False, payment_pending=True,
                              now=datetime(2026, 11, 2, 13, tzinfo=timezone.utc))
        self.assertIn('d0befb68', order['state']['meta']['href'])
        self.assertNotIn('deliveryPlannedMoment', order)
        with self.assertRaisesRegex(ValueError, 'payment_pending_only'):
            build_payload(platform='ozon', account='acc1', external_id='x',
                          lines=[Line('p', Decimal(1), 100)], expected_kopeks=100,
                          available=False, payment_pending=True,
                          now=datetime.now(timezone.utc))

    def test_rejects_total_mismatch_and_yandex_acc2(self):
        kwargs = dict(platform='wb', account='acc1', external_id='123',
                      lines=[Line('p', Decimal(1), 100)], expected_kopeks=101,
                      available=False, now=datetime.now(timezone.utc))
        with self.assertRaisesRegex(ValueError, 'total_mismatch'):
            build_payload(**kwargs)
        kwargs.update(platform='yandex', account='acc2')
        with self.assertRaisesRegex(ValueError, 'yandex_acc2'):
            build_payload(**kwargs)


if __name__ == '__main__':
    unittest.main()
