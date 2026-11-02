# поток: mkt
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from ops import ya_promo_guard as guard


class FixedRetentionTests(TestCase):
    def test_snapshot_uses_approved_72_percent_without_finance_basis(self):
        with (patch.object(guard.q, 'yandex_retention', side_effect=AssertionError('finance basis used')),
              patch.object(guard, 'promos', return_value=[]),
              patch.object(guard.g, 'cost_map', return_value={})):
            rows, acts, skipped, keep, source = guard.snapshot()
        self.assertEqual((rows, acts, skipped), ([], [], []))
        self.assertEqual(keep, Decimal('0.28'))
        self.assertIn('72%', source)

    def test_limit_keeps_one_offer_across_promotions(self):
        rows = [
            {'offer_id': '0019', 'mode': 'raise', 'floor_price': Decimal('100'), 'promo_price': 50.0, 'reason': ''},
            {'offer_id': '0019', 'mode': 'remove', 'floor_price': 100, 'promo_price': 55, 'reason': ''},
            {'offer_id': '0020', 'mode': 'raise', 'floor_price': 100, 'promo_price': 80, 'reason': ''},
        ]
        guard._cut(rows, 1)
        self.assertEqual([row['mode'] for row in rows], ['raise', 'remove', 'ok_deferred'])
