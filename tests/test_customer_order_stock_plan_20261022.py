# поток: inv
import unittest
from unittest.mock import patch
from decimal import Decimal

from tools import customer_order_stock_plan_20261022 as m


def card(cid, code='1000', supplier='s1', brand='T2'):
    return {'id': cid, 'externalCode': code, 'name': 'Cartridge ' + cid,
        'meta': {'href': 'https://api.moysklad.ru/api/remap/1.2/entity/product/' + cid, 'type': 'product'},
        'supplier': {'name': supplier, 'meta': {'href': 'https://example/entity/counterparty/' + supplier}},
        'attributes': [{'name': 'Бренд', 'value': brand}]}


def order(current, reserve=0):
    return {'store': {'id': 'w'}, 'applicable': True,
        'positions': {'rows': [{'id': 'line', 'assortment': current, 'quantity': 1,
                                'price': 12345, 'discount': 0, 'reserve': reserve}]}}


class StockPlanTests(unittest.TestCase):
    def setUp(self):
        self.tc = patch.object(m, 'tc', side_effect=lambda code: [] if code == '9000' else {'error': 'not_mix'}).start()
        self.addCleanup(patch.stopall)

    def test_own_reserve_can_be_used_but_stock_cannot_be_promised_twice(self):
        a = card('a')
        p = m.Planner({'a': a}, {'w/a': {'stock': 1, 'reserve': 1}}, {'1000': {'1000': 1}})
        first = p.order(order(a, reserve=1), p.initial, {})
        self.assertTrue(first['ready'])
        second = p.order(order(a), first['pool'], first['used'])
        self.assertFalse(second['ready'])

    def test_expected_delivery_is_not_physical_stock(self):
        a = card('a')
        p = m.Planner({'a': a}, {'w/a': {'stock': 0, 'inTransit': 100}}, {'1000': {'1000': 1}})
        self.assertFalse(p.order(order(a), p.initial, {})['ready'])

    def test_odyssey_aquamarine_brand_from_explicit_article_and_code(self):
        a = card('a'); a['attributes'] = []
        a.update(article='AT-TEST', code='1000at')
        a['supplier']['name'] = 'ООО "ОДИССЕЙ"'
        self.assertEqual(m.brand(a)[:2], ('at', 'AQUAMARINE'))

    def test_other_supplier_at_article_does_not_prove_aquamarine_brand(self):
        a = card('a'); a['attributes'] = []
        a.update(article='AT-TEST', code='1000at')
        self.assertEqual(m.brand(a)[0], '')

    def test_full_supplier_and_brand_preferred_over_abundant_mixed_stock(self):
        parent = card('parent', '9000')
        cards = {'parent': parent, 'a': card('a', '1000'), 'b': card('b', '2000'),
                 'x': card('x', '1000', 's2', 'EP'), 'y': card('y', '2000', 's3', 'HB')}
        stock = {'w/' + cid: {'stock': 10 if cid in ('x', 'y') else 1, 'reserve': 0} for cid in cards}
        p = m.Planner(cards, stock, {'9000': {'1000': 1, '2000': 1}})
        r = p.order(order(parent), p.initial, {})
        self.assertTrue(r['ready'])
        self.assertEqual({x['id'] for x in r['lines'][0]['selected']}, {'a', 'b'})
        self.assertEqual(r['lines'][0]['mode'], 'Набор: один поставщик и бренд')

    def test_same_brand_across_suppliers_when_full_supplier_unavailable(self):
        parent = card('parent', '9000')
        cards = {'parent': parent, 'a': card('a', '1000', 's1'), 'b': card('b', '2000', 's2')}
        p = m.Planner(cards, {'w/a': {'stock': 1}, 'w/b': {'stock': 1}}, {'9000': {'1000': 1, '2000': 1}})
        r = p.order(order(parent), p.initial, {})
        self.assertTrue(r['ready'])
        self.assertEqual(r['lines'][0]['mode'], 'Набор: один бренд, разные поставщики')

    def test_failed_order_does_not_consume_stock(self):
        a, b = card('a'), card('b', '2000')
        p = m.Planner({'a': a, 'b': b}, {'w/a': {'stock': 1}}, {'1000': {'1000': 1}, '2000': {'2000': 1}})
        o = order(a); o['positions']['rows'] += order(b)['positions']['rows']
        before = dict(p.initial)
        self.assertFalse(p.order(o, p.initial, {})['ready'])
        self.assertEqual(p.initial, before)

    def test_bundle_only_mode_does_not_replace_unavailable_regular_product(self):
        a, b = card('a'), card('b', supplier='s2')
        p = m.Planner({'a': a, 'b': b}, {'w/b': {'stock': 1}}, {'1000': {'1000': 1}},
                      set_codes=set(), allow_single_replacements=False)
        self.assertFalse(p.order(order(a), p.initial, {})['ready'])

    def test_fresh_set_kind_does_not_read_older_tc_cache(self):
        parent, a = card('parent', '9000'), card('a')
        self.tc.side_effect = RuntimeError('stale catalogue must not be read')
        p = m.Planner({'parent': parent, 'a': a}, {'w/a': {'stock': 1}},
                      {'9000': {'1000': 1}}, set_codes={'9000'}, allow_single_replacements=False)
        self.assertTrue(p.order(order(parent), p.initial, {})['ready'])

    def test_set_discount_and_price_preserved_to_kopeck(self):
        parent = card('parent', '9000'); a = card('a')
        source = order(parent)['positions']['rows'][0]; source['discount'] = 12.11
        line = {'source': source, 'is_set': True, 'selected': [{'id': 'a', 'quantity': 4}]}
        o = {'vatEnabled': True, 'vatIncluded': True}
        rows = m.expanded_positions(line, o, {'a': a})
        self.assertEqual(sum(m.num(r['quantity']) for r in rows), Decimal(4))
        self.assertEqual(sum(m.num(r['price']) for r in rows), m.num(source['price']))
        self.assertEqual(sum(m.line_sum(r, o) for r in rows), m.line_sum(source, o))

    def test_multiple_rounding_pairs_preserve_two_kopeck_discount_difference(self):
        parent = card('parent', '9000'); a = card('a')
        source = order(parent)['positions']['rows'][0]
        source.update(price=4259300, discount=27.72, vat=5)
        line = {'source': source, 'is_set': True, 'selected': [{'id': 'a', 'quantity': 4}]}
        o = {'vatEnabled': True, 'vatIncluded': True}
        rows = m.expanded_positions(line, o, {'a': a})
        self.assertEqual(sum(m.num(r['price']) for r in rows), Decimal(4259300))
        self.assertEqual(sum(m.line_sum(r, o) for r in rows), Decimal(3078622))

    def test_split_preserves_vat_sum_when_equal_parts_round_up_or_down(self):
        for price, vat_amount in [(1124900, 53567), (1238700, 58986)]:
            with self.subTest(price=price):
                source = order(card('parent', '9000'))['positions']['rows'][0]
                source.update(price=price, discount=0, vat=5, vatEnabled=True)
                line = {'source': source, 'is_set': True, 'selected': [{'id': 'a', 'quantity': 4}]}
                o = {'vatEnabled': True, 'vatIncluded': True}
                rows = m.expanded_positions(line, o, {'a': card('a')})
                self.assertEqual(sum(m.num(r['price']) for r in rows), Decimal(price))
                self.assertEqual(sum(m.line_sum(r, o) for r in rows), Decimal(price))
                self.assertEqual(sum(m.line_vat(r, o) for r in rows), Decimal(vat_amount))


if __name__ == '__main__':
    unittest.main()
