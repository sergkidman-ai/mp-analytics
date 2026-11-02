# поток: inv
from unittest import TestCase

from ops.fbs_ms_set_catalog import components


class SetCatalogTests(TestCase):
    def test_nested_and_repeated_components(self):
        mapping = {'4297': ('2860', '2861', '2861', '3795'),
                   '3795': ('3791', '3792')}
        self.assertEqual(components('4297', mapping),
                         [('2860', 1), ('2861', 2), ('3791', 1), ('3792', 1)])

    def test_unknown_or_cyclic_set(self):
        self.assertIsNone(components('0019', {'4297': ('2860',)}))
        with self.assertRaisesRegex(ValueError, 'set_composition_cycle'):
            components('4297', {'4297': ('3795',), '3795': ('4297',)})
