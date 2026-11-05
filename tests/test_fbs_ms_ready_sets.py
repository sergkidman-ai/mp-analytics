# поток: inv
"""Whole-kit stock must precede component allocation without double booking."""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import os
from unittest import TestCase
from unittest.mock import patch

from ops import fbs_ms_import_next5 as importer
from ops.fbs_ms_import_policy import Card, SelectionError, choose_ready_set
from ops.fbs_ms_order_payload import STORE
from ops.fbs_ms_post_import_status import imported_availability, provenance
from prices.profiles import STORE_REMOTE
from tests.test_fbs_ms_import_set_integration import product


class ReadySetTests(TestCase):
    def inspect(self, *, quantity=1, stock=1, reserve=0, promised=None,
                repeat=False, supplier=True, composition=True, account='acc1', component_stock=50):
        parent = dict(product('0168'), name='Набор картриджей', code='0168cs')
        if not supplier:
            parent.pop('supplier')
        source = {'platform': 'ozon', 'account': account, 'number': '57808553-0147-1',
                  'created_at': '2026-11-05T10:00:00+03:00', 'status': 'awaiting_packaging',
                  'lines': [{'market_code': '0168', 'quantity': quantity,
                             'price_kopeks': 100000, 'discount_percent': '10'}]}
        if repeat:
            source['lines'] *= 2

        def cards(code):
            return [parent if code == '0168' else product(code)]

        def warehouse(store, cards):
            return {c['id']: {'stock': stock if c['externalCode'] == '0168' else component_stock,
                              'reserve': reserve if c['externalCode'] == '0168' else 0}
                    for c in cards}

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'FBS_SET_SPLIT_ENABLED': '1'}))
            stack.enter_context(patch.object(importer, 'source', return_value=source))
            stack.enter_context(patch.object(importer, 'find_customerorder', return_value=None))
            stack.enter_context(patch.object(importer, 'set_components',
                **({'return_value': [('0161', 1), ('0162', 1)]} if composition else
                   {'side_effect': RuntimeError('catalog unavailable')})))
            card_reads = stack.enter_context(patch.object(importer, 'product_cards', side_effect=cards))
            stock_reads = stack.enter_context(patch.object(importer, 'warehouse_stock', side_effect=warehouse))
            result = importer.inspect({'number': source['number']}, {},
                {'id-0161': 20000, 'id-0162': 30000}, 'digest', promised)
        return result, card_reads, stock_reads

    def test_whole_set_precedes_components_and_preserves_sale(self):
        result, cards, stock = self.inspect(quantity=2, stock=2)
        self.assertEqual([x['base_code'] for x in result['selected']], ['0168'])
        self.assertTrue(result['selected'][0]['whole_set'])
        position, = result['payload']['positions']
        self.assertEqual(position['quantity'], 2)
        self.assertEqual(position['price'], 100000)
        self.assertEqual(position['discount'], 10)
        self.assertEqual(result['total_kopeks'], 180000)
        cards.assert_called_once_with('0168')
        self.assertEqual([call.args[0] for call in stock.call_args_list], [STORE['acc1']])

    def test_assembled_set_needs_no_supplier_or_purchase_price(self):
        result, _, _ = self.inspect(supplier=False)
        self.assertEqual(imported_availability(result)[0], True)
        self.assertEqual(provenance(result), (False, False))
        self.assertIsNone(result['selected'][0]['purchase_kopeks'])

    def test_reserved_set_and_insufficient_quantity_use_components(self):
        for kwargs in ({'stock': 1, 'reserve': 1}, {'stock': 1, 'quantity': 2}):
            with self.subTest(kwargs=kwargs):
                result, _, _ = self.inspect(**kwargs)
                self.assertTrue(all(x.get('component_of') == '0168' for x in result['selected']))
                self.assertEqual(len(result['selected']), kwargs.get('quantity', 1) * 2)
                self.assertTrue(all(x['available'] for x in result['selected']))

    def test_batch_promises_deducted_without_mutating_caller(self):
        promised = {(STORE['acc1'], 'id-0168'): Decimal(1)}
        before = deepcopy(promised)
        first, _, _ = self.inspect(promised=promised)
        second, _, _ = self.inspect(promised=promised)
        self.assertEqual(first, second)
        self.assertTrue(all(x.get('component_of') == '0168' for x in first['selected']))
        self.assertEqual(promised, before)

    def test_two_lines_cannot_reuse_single_whole_set(self):
        result, _, _ = self.inspect(repeat=True)
        self.assertEqual([x['base_code'] for x in result['selected']], ['0168', '0161', '0162'])
        self.assertEqual(result['total_kopeks'], 180000)

    def test_ready_whole_set_still_works_when_catalog_unavailable(self):
        result, cards, _ = self.inspect(composition=False)
        self.assertTrue(result['selected'][0]['whole_set'])
        cards.assert_called_once_with('0168')

    def test_account_warehouse_and_promises_are_kept_separate(self):
        result, _, reads = self.inspect(account='acc2',
            promised={(STORE['acc1'], 'id-0168'): Decimal(10)})
        self.assertTrue(result['selected'][0]['whole_set'])
        self.assertEqual([c.args[0] for c in reads.call_args_list], [STORE['acc2']])
        self.assertNotEqual(STORE['acc2'], STORE_REMOTE)

    def test_archived_and_wrong_code_stock_are_not_whole_set_evidence(self):
        eligible = Card('valid', '0168', '', '', None, Decimal(1))
        wrong = Card('wrong', '9999', '', '', None, Decimal(100))
        archived = Card('archived', '0168', '', '', None, Decimal(100), archived=True)
        self.assertEqual(choose_ready_set([eligible, wrong, archived], '0168', Decimal(1)), eligible)
        with self.assertRaises(SelectionError):
            choose_ready_set([wrong, archived], '0168', Decimal(1))

    def test_two_component_orders_cannot_reuse_same_own_units(self):
        result, _, _ = self.inspect(stock=0, repeat=True, component_stock=1)
        self.assertEqual([x['available'] for x in result['selected']], [True, True, False, False])
        self.assertEqual([x['stock_store_id'] for x in result['selected']],
                         [STORE['acc1'], STORE['acc1'], STORE_REMOTE, STORE_REMOTE])

    def test_missing_own_whole_set_splits_without_remote_parent_lookup(self):
        result, _, reads = self.inspect(stock=0)
        self.assertTrue(all(x.get('component_of') == '0168' for x in result['selected']))
        remote_ids = [card['externalCode'] for call in reads.call_args_list
                      if call.args[0] == STORE_REMOTE for card in call.args[1]]
        self.assertNotIn('0168', remote_ids)
