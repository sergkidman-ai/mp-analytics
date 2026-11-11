# поток: inv
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from core import ms_api

from ops import fbs_ms_import_scheduler as scheduler
from ops import fbs_ms_import_next5 as importer
from ops.fbs_ms_import_wb_pilot import current_purchase_prices, current_purchase_documents
from ops.fbs_ms_import_wb_pilot import purchase_documents_digest
from prices.profiles import STORE_REMOTE


def receipt(uid, name, updated='v1', **extra):
    return {'id': uid, 'name': name, 'updated': updated,
            'applicable': True, 'store': {'meta': {'href':
                'https://example/entity/store/' + STORE_REMOTE}}, **extra}


def position(pid, price):
    return {'assortment': {'meta': {'href': 'https://example/product/' + pid}}, 'price': price}


class AllSupplierPriceTests(unittest.TestCase):
    def test_scheduler_persists_only_the_verified_final_generation(self):
        with TemporaryDirectory() as tmp, \
             patch.object(scheduler, 'OUT', Path(tmp)), \
             patch.object(scheduler, 'set_catalog', return_value={}), \
             patch.object(scheduler, 'enter_digest', return_value='before'), \
             patch.object(scheduler, 'product_cards', return_value=[{'id': 'p'}]), \
             patch.object(scheduler, 'current_purchase_prices') as scan:
            rows = [{'number': 'n', 'products': [{'code': '1234'}]}]
            scan.side_effect = RuntimeError('enter_changed_during_price_scan')
            with self.assertRaises(RuntimeError):
                scheduler.prices_for(rows)
            cache = Path(tmp) / 'prices_snapshot_all.json'
            self.assertFalse(cache.exists())
            scan.side_effect = None
            scan.return_value = ({'p': 200}, 'verified-final')
            self.assertEqual(scheduler.prices_for(rows), ({'p': 200}, 'verified-final'))
            self.assertEqual(json.loads(cache.read_text())['digest'], 'verified-final')

    def test_deleted_receipt_replaced_without_keeping_partial_prices(self):
        stable = ('bulat', '2026-11-03', receipt('stable', 'bulat_2026-11-03'))
        old = ('sakura', '2026-11-03', receipt('old', 'sakura_2026-11-03'))
        new = ('sakura', '2026-11-06', receipt('new', 'sakura_2026-11-06'))
        calls = []
        def get(path, params):
            uid = path.split('/')[-2]; calls.append(uid)
            if uid == 'old':
                raise ms_api.MsError(404, {})
            rows = [position(uid, 100 if uid == 'stable' else 200)]
            return {'meta': {'size': 1}, 'rows': rows}
        with patch('ops.fbs_ms_import_wb_pilot.current_purchase_documents',
                   side_effect=[[stable, old], [stable, new], [stable, new]]), \
             patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=get):
            prices, digest = current_purchase_prices(None)
        self.assertEqual(prices, {'stable': 100, 'new': 200})
        self.assertEqual(digest, purchase_documents_digest([stable, new]))
        self.assertEqual(calls.count('stable'), 1)

    def test_in_place_update_reloads_price_before_returning(self):
        old = [('sakura', '2026-11-03', receipt('sk', 'sakura_2026-11-03'))]
        new = [('sakura', '2026-11-03', receipt('sk', 'sakura_2026-11-03', updated='v2'))]
        def page(price):
            return {'meta': {'size': 1}, 'rows': [position('p', price)]}
        with patch('ops.fbs_ms_import_wb_pilot.current_purchase_documents',
                   side_effect=[old, new, new]), \
             patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=[page(100), page(200)]) as get:
            prices, digest = current_purchase_prices({'p'})
        self.assertEqual(prices, {'p': 200})
        self.assertEqual(digest, purchase_documents_digest(new))
        self.assertEqual(get.call_count, 2)

    def test_stable_missing_receipt_fails_instead_of_omitting_supplier(self):
        docs = [('sakura', '2026-11-03', receipt('sk', 'sakura_2026-11-03'))]
        with patch('ops.fbs_ms_import_wb_pilot.current_purchase_documents', return_value=docs), \
             patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=ms_api.MsError(404, {})):
            with self.assertRaises(ms_api.MsError):
                current_purchase_prices(None)

    def test_continuous_replacements_stop_after_bounded_attempts(self):
        versions = [[('sakura', '2026-11-03', receipt(str(i), 'sakura_2026-11-03'))]
                    for i in range(4)]
        with patch('ops.fbs_ms_import_wb_pilot.current_purchase_documents', side_effect=versions), \
             patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=ms_api.MsError(404, {})) as get:
            with self.assertRaisesRegex(RuntimeError, 'enter_changed_during_price_scan'):
                current_purchase_prices(None)
        self.assertEqual(get.call_count, 3)

    def run_prices(self, docs, positions):
        def get(path, params=None):
            if path == '/entity/enter':
                self.assertNotIn('moment>=', str(params))
                return {'meta': {'size': len(docs)}, 'rows': docs}
            rows = positions[path.split('/')[-2]]
            return {'meta': {'size': len(rows)}, 'rows': rows}
        with patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=get):
            prices, digest = current_purchase_prices(None)
            self.assertEqual(digest, scheduler.enter_digest())
        return prices, digest

    def test_sakura_and_other_supplier_groups_are_included(self):
        docs = [receipt(key, key + '_2026-11-03_p1')
                for key in ('sakura', 'bulat', 's_print_msk', 'colortek', 'rapid', 'easy_print')]
        positions = {doc['id']: [position(doc['id'], 10000)] for doc in docs}
        prices, _ = self.run_prices(docs, positions)
        self.assertEqual(prices, {doc['id']: 10000 for doc in docs})

    def test_latest_only_all_pages_no_suffix_old_and_unapplied_excluded(self):
        docs = [receipt('old', 'sakura_2026-10-28_p1'),
                receipt('new1', 'sakura_2026-10-29_p1'),
                receipt('new2', 'sakura_2026-10-29_p2'),
                receipt('retired', 'sakura_2026-11-03_p1_old1'),
                receipt('draft', 'sakura_2026-11-04_p1', applicable=False),
                receipt('plain', 'bulat_2026-10-20')]
        prices, _ = self.run_prices(docs, {'new1': [position('a', 100)],
            'new2': [position('b', 200)], 'plain': [position('c', 300)]})
        self.assertEqual(prices, {'a': 100, 'b': 200, 'c': 300})

    def test_sakura_update_invalidates_cache_digest(self):
        docs = [receipt('sk', 'sakura_2026-11-03_p1')]
        _, before = self.run_prices(docs, {'sk': [position('sk', 50000)]})
        docs[0]['updated'] = 'v2'
        _, after = self.run_prices(docs, {'sk': [position('sk', 40000)]})
        self.assertNotEqual(before, after)

    def test_invalid_unrelated_prices_do_not_break_full_cache_or_restore_bad_card(self):
        docs = [receipt('sk', 'sakura_2026-11-03_p1')]
        prices, _ = self.run_prices(docs, {'sk': [position('good', 50000),
            position('bad', 100), position('bad', None), position('nan', 'NaN'),
            position('bad', 100)]})
        self.assertEqual(prices, {'good': 50000})

    def test_document_list_pagination_is_complete(self):
        docs = [receipt('a', 'sakura_2026-11-03_p1'), receipt('b', 'bulat_2026-11-03_p1')]
        def get(path, params):
            self.assertEqual(path, '/entity/enter')
            return {'meta': {'size': 2}, 'rows': [docs[params['offset']]]}
        with patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=get):
            selected = current_purchase_documents()
        self.assertEqual({d['id'] for _, _, d in selected}, {'a', 'b'})

    def test_missing_page_and_wrong_store_fail_closed(self):
        with patch('ops.fbs_ms_import_wb_pilot.ms_api.get',
                   return_value={'meta': {'size': 2}, 'rows': []}):
            with self.assertRaisesRegex(RuntimeError, 'enter_list_incomplete'):
                current_purchase_documents()
        docs = [receipt('wrong', 'sakura_2026-11-03_p1',
                        store={'meta': {'href': 'https://example/entity/store/other'}})]
        with patch('ops.fbs_ms_import_wb_pilot.ms_api.get',
                   return_value={'meta': {'size': 1}, 'rows': docs}):
            with self.assertRaisesRegex(RuntimeError, 'enter_store_mismatch'):
                current_purchase_documents()


if __name__ == '__main__':
    unittest.main()
