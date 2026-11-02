# поток: inv
import unittest

from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock


class ReadTests(unittest.TestCase):
    def test_full_number_search_all_statuses_and_ambiguity(self):
        calls = []
        def get(path, params):
            calls.append((path, params))
            return {'meta': {'size': 1}, 'rows': [{'name': '0109157099-0048-1', 'id': 'x'}]}
        self.assertEqual(find_customerorder('0109157099-0048-1', get)['id'], 'x')
        self.assertNotIn('state', str(calls[0][1]))
        def duplicate(path, params):
            return {'meta': {'size': 2}, 'rows': [{'name': '42'}, {'name': '42'}]}
        with self.assertRaisesRegex(RuntimeError, 'duplicate_customerorder'):
            find_customerorder('42', duplicate)

    def test_product_exact_code_and_complete_pages(self):
        def get(path, params):
            if params['offset'] == 0:
                return {'meta': {'size': 2}, 'rows': [{'externalCode': '0019',
                    'meta': {'type': 'product'}, 'id': 'a'}]}
            return {'meta': {'size': 2}, 'rows': [{'externalCode': '00190',
                'meta': {'type': 'product'}, 'id': 'b'}]}
        self.assertEqual([x['id'] for x in product_cards('0019', get)], ['a'])

    def test_stock_missing_row_is_zero_and_other_card_blocks(self):
        c = {'id': 'a', 'meta': {'href': 'https://example/product/a'}}
        def get(path, params):
            return {'meta': {'size': 0}, 'rows': []}
        self.assertEqual(warehouse_stock('store-id', [c], get),
                         {'a': {'stock': 0, 'reserve': 0}})
        def wrong(path, params):
            return {'meta': {'size': 1}, 'rows': [{'meta': {'href': 'https://example/product/b'},
                    'stock': 1, 'reserve': 0}]}
        with self.assertRaisesRegex(RuntimeError, 'stock_response_identity'):
            warehouse_stock('store-id', [c], wrong)


if __name__ == '__main__':
    unittest.main()
