# поток: mkt
from unittest import TestCase

from unittest.mock import patch

from ops.promo_ordinary_scope import confirmed, confirmed_set, looks_like_set, verified_set_codes
from ops.ozon_promo_guard import coordinate_shared_prices


class OrdinaryScopeTests(TestCase):
    def test_set_and_bundle_codes_are_excluded(self):
        allowed = {'0019', '0274'}
        self.assertFalse(confirmed('0274msk', '', allowed))
        self.assertFalse(confirmed('0019x2', '', allowed))
        self.assertFalse(confirmed('0019wb', 'Набор картриджей', allowed))
        self.assertTrue(looks_like_set('0019X6', None))
        self.assertTrue(looks_like_set('WB-set-0019', None))

    def test_confirmed_ordinary_variant(self):
        self.assertTrue(confirmed('0019wb', 'Картридж 0019', {'0019'}))
        self.assertFalse(confirmed('0019wb', 'Картридж 0019', set()))

    def test_verified_kit_requires_exact_identity_or_kit_marker(self):
        sets = {'4297'}
        self.assertTrue(confirmed_set('4297', sets))
        self.assertTrue(confirmed_set('4297msk', sets))
        self.assertTrue(confirmed_set('4297wb', sets, 'Набор картриджей'))
        self.assertFalse(confirmed_set('4297wb', sets, 'Картридж'))
        self.assertFalse(confirmed_set('4297', set()))

    @patch('ops.promo_ordinary_scope.db.query')
    def test_incomplete_kit_composition_is_excluded(self, query):
        query.return_value = [
            {'external_code': '4297', 'components': ['2860', '2861'], 'n_components': 2},
            {'external_code': '3795', 'components': ['3791'], 'n_components': 2},
            {'external_code': '1234', 'components': ['oops'], 'n_components': 1},
        ]
        self.assertEqual(verified_set_codes(), {'4297'})

    def test_shared_ozon_product_with_unverified_row_stays_skipped(self):
        rows = [
            {'product_id': 1, 'mode': 'raise', 'reason': '', 'is_current': True},
            {'product_id': 1, 'mode': 'skip_unverified', 'reason': '', 'is_current': True},
        ]
        coordinate_shared_prices(rows)
        self.assertEqual([row['mode'] for row in rows], ['skip_unverified', 'skip_unverified'])
