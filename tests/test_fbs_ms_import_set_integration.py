# поток: inv
from unittest import TestCase
from unittest.mock import patch
import os

from ops import fbs_ms_import_next5 as importer
from ops.fbs_ms_order_payload import STORE
from prices.profiles import STORE_REMOTE


SUPPLIER = '61d01266-2fe1-11f0-0a80-0f7f000bf967'


def product(code):
    uid = 'id-' + code
    return {'id': uid, 'externalCode': code, 'code': code + 'wb', 'name': 'Картридж',
            'meta': {'type': 'product', 'href': 'https://example/entity/product/' + uid},
            'supplier': {'meta': {'href': 'https://example/entity/counterparty/' + SUPPLIER}}}


class ImportSetIntegrationTests(TestCase):
    def test_confirmed_set_becomes_component_lines_and_can_be_confirmed(self):
        source = {'platform': 'ozon', 'account': 'acc1', 'number': '123-0001-1',
                  'created_at': '2026-11-02T10:00:00+03:00', 'status': 'awaiting_packaging',
                  'lines': [{'market_code': '4297', 'quantity': 1,
                             'price_kopeks': 100000, 'discount_percent': '10'}]}

        def cards(code):
            if code == '4297':
                return []
            self.assertIn(code, ('2860', '2861'))
            return [product(code)]

        def stock(store, cards):
            return {c['id']: {'stock': 1 if store == STORE['acc1'] else 5,
                              'reserve': 0} for c in cards}

        with patch.object(importer, 'source', return_value=source), \
             patch.dict(os.environ, {'FBS_SET_SPLIT_ENABLED': '1'}), \
             patch.object(importer, 'find_customerorder', return_value=None), \
             patch.object(importer, 'set_components', return_value=[('2860', 1), ('2861', 1)]), \
             patch.object(importer, 'product_cards', side_effect=cards), \
             patch.object(importer, 'warehouse_stock', side_effect=stock):
            result = importer.inspect({'number': source['number']}, {},
                                      {'id-2860': 20000, 'id-2861': 30000}, 'digest')
        self.assertEqual([x['base_code'] for x in result['selected']], ['2860', '2861'])
        self.assertTrue(all(x['available'] for x in result['selected']))
        self.assertEqual(len(result['payload']['positions']), 2)
        self.assertEqual(result['total_kopeks'], 90000)

    def test_missing_component_keeps_parent_for_manual_review(self):
        source = {'platform': 'ozon', 'account': 'acc1', 'number': '123-0001-1',
                  'created_at': '2026-11-02T10:00:00+03:00', 'status': 'awaiting_packaging',
                  'lines': [{'market_code': '4297', 'quantity': 1,
                             'price_kopeks': 100000, 'discount_percent': '0'}]}

        def cards(code):
            return [product(code)] if code in ('2860', '4297') else []

        def stock(store, cards):
            return {c['id']: {'stock': 1 if store == STORE['acc1'] and c['externalCode'] != '4297' else 0,
                              'reserve': 0} for c in cards}

        with patch.object(importer, 'source', return_value=source), \
             patch.dict(os.environ, {'FBS_SET_SPLIT_ENABLED': '1'}), \
             patch.object(importer, 'find_customerorder', return_value=None), \
             patch.object(importer, 'set_components', return_value=[('2860', 1), ('2861', 1)]), \
             patch.object(importer, 'product_cards', side_effect=cards), \
             patch.object(importer, 'warehouse_stock', side_effect=stock):
            result = importer.inspect({'number': source['number']}, {},
                                      {'id-4297': 10000}, 'digest')
        self.assertEqual(len(result['selected']), 1)
        self.assertEqual(result['selected'][0]['card_code'], '4297wb')
        self.assertTrue(result['selected'][0]['manual_parent'])
        self.assertFalse(result['selected'][0]['available'])

    def test_merged_ms_component_lines_have_same_identity_and_quantity(self):
        href = 'https://example/entity/product/p'
        separate = [{'assortment': {'meta': {'href': href}}, 'quantity': 1},
                    {'assortment': {'meta': {'href': href}}, 'quantity': 1}]
        merged = [{'assortment': {'meta': {'href': href}}, 'quantity': 2}]
        self.assertEqual(importer._quantities(separate), importer._quantities(merged))

    def test_derivative_of_set_family_requires_exact_kit_card(self):
        ordinary = dict(product('0168'), code='0168sk', name='Струйный картридж')
        kit = dict(product('0168'), code='0168msk', name='Набор картриджей')
        self.assertFalse(importer._confirmed_set_offer('0168sk', '0168', [ordinary, kit]))
        self.assertTrue(importer._confirmed_set_offer('0168msk', '0168', [ordinary, kit]))
        self.assertFalse(importer._confirmed_set_offer('01681', '0168', [ordinary, kit]))

    def test_dsk_generated_set_alias_uses_exact_parent_without_name_guess(self):
        parent = dict(product('6282'), code='6282dsk', name='Картриджи лазерные')
        self.assertTrue(importer._confirmed_set_offer('6282AMUGB99G','6282',[parent],account='acc2'))
        self.assertFalse(importer._confirmed_set_offer('6282AMUGB99G','6282',[parent],account='acc1'))
        self.assertFalse(importer._confirmed_set_offer('6282AMUGB99G','6282',[],account='acc2'))
        self.assertFalse(importer._confirmed_set_offer('628212345678','6282',[parent],account='acc2'))
        ordinary = dict(product('6282'),code='6282AMUGB99G',name='Одиночный картридж')
        self.assertFalse(importer._confirmed_set_offer('6282AMUGB99G','6282',[parent,ordinary],account='acc2'))

    def test_dsk_generated_alias_enters_component_stock_and_status_path(self):
        source = {'platform':'ozon','account':'acc2','number':'123-0001-1',
                  'created_at':'2026-11-03T10:00:00+03:00','status':'awaiting_packaging',
                  'lines':[{'market_code':'6282AMUGB99G','quantity':2,
                            'price_kopeks':100000,'discount_percent':'10'}]}
        def cards(code):
            if code=='6282':return [dict(product(code),code=code+'dsk',name='Картриджи лазерные')]
            return [product(code)]
        def stock(store, cards):
            self.assertIn(store,(STORE['acc2'],STORE_REMOTE))
            return {c['id']:{'stock':0 if c['externalCode']=='6282' else 2,'reserve':0} for c in cards}
        with patch.object(importer,'source',return_value=source), \
             patch.dict(os.environ,{'FBS_SET_SPLIT_ENABLED':'1'}), \
             patch.object(importer,'find_customerorder',return_value=None), \
             patch.object(importer,'set_components',return_value=[('2860',1),('2861',1)]), \
             patch.object(importer,'product_cards',side_effect=cards), \
             patch.object(importer,'warehouse_stock',side_effect=stock):
            result=importer.inspect({'number':source['number']},{},{'id-2860':20000,'id-2861':30000},'digest')
        self.assertEqual(len(result['payload']['positions']),4)
        self.assertTrue(all(s.get('component_of')=='6282' and s['available'] for s in result['selected']))
        self.assertEqual(result['total_kopeks'],180000)

    def test_promised_component_is_not_allocated_twice_in_batch(self):
        def cards(code):
            return [product(code)]
        def stock(store, cards):
            return {c['id']: {'stock': 1 if store == STORE['acc1'] else 5,
                              'reserve': 0} for c in cards}
        with patch.object(importer, 'product_cards', side_effect=cards), \
             patch.object(importer, 'warehouse_stock', side_effect=stock):
            _, proof = importer._kit_lines(
                {'market_code': '4297', 'quantity': 1, 'price_kopeks': 100000,
                 'discount_percent': '0'}, '4297', 'acc1',
                {'id-2860': 20000, 'id-2861': 30000},
                [('2860', 1), ('2861', 1)], {(STORE['acc1'], 'id-2860'): 1})
        self.assertEqual([(x['base_code'], x['available']) for x in proof],
                         [('2860', False), ('2861', True)])

    def test_remote_kit_receipt_change_stops_before_post(self):
        proof = {'market': {'number': 'dry-receipt'},
                 'selected': [{'component_of': '4297', 'available': False}],
                 'total_kopeks': 100, 'prices_digest': 'old', 'payload': {}}
        with patch.object(importer, 'inspect', return_value=proof), \
             patch.object(importer, 'save'), \
             patch('ops.fbs_ms_import_scheduler.enter_digest', return_value='new'), \
             patch.object(importer.requests, 'post') as post:
            with self.assertRaisesRegex(RuntimeError, 'set_purchase_receipt_changed_before_post'):
                importer.create_one({'number': 'dry-receipt'}, {}, {}, 'old')
        post.assert_not_called()

    def test_kit_quantity_multiplies_every_component(self):
        def cards(code):
            return [product(code)]
        def stock(store, cards):
            return {c['id']: {'stock': 4, 'reserve': 0} for c in cards}
        with patch.object(importer, 'product_cards', side_effect=cards), \
             patch.object(importer, 'warehouse_stock', side_effect=stock):
            lines, proof = importer._kit_lines(
                {'market_code': '4297', 'quantity': 2, 'price_kopeks': 100000,
                 'discount_percent': '10'}, '4297', 'acc1',
                {'id-2860': 20000, 'id-2861': 30000},
                [('2860', 2), ('2861', 1)])
        self.assertEqual([x['base_code'] for x in proof].count('2860'), 4)
        self.assertEqual([x['base_code'] for x in proof].count('2861'), 2)
        self.assertEqual(sum(importer.line_total(x) for x in lines), 180000)

    def test_ms_own_reserve_is_not_counted_again_as_local_promise(self):
        own = STORE['acc1']
        selected = [{'stock_store_id': own, 'card_id': 'a', 'quantity': '2'},
                    {'stock_store_id': STORE_REMOTE, 'card_id': 'b', 'quantity': '1'}]
        positions = [{'assortment': {'id': 'a'}, 'reserve': 2},
                     {'assortment': {'id': 'b'}, 'reserve': 0}]
        self.assertEqual(importer._unreserved_promises(selected, positions, own),
                         {(own, 'a'): 0, (STORE_REMOTE, 'b'): 1})

    def test_unknown_set_uses_existing_exact_code_card_for_manual_review(self):
        source = {'platform': 'wb', 'account': 'acc1', 'number': '12345',
                  'created_at': '2026-11-02T10:00:00+03:00', 'status': ['new', 'waiting'],
                  'lines': [{'market_code': '4297', 'quantity': 1,
                             'price_kopeks': 100000, 'discount_percent': '0'}]}
        card = dict(product('4297'), code='4297zz', supplier={'meta': {'href': 'https://example/x/unknown'}})
        with patch.object(importer, 'source', return_value=source), \
             patch.object(importer, 'find_customerorder', return_value=None), \
             patch.object(importer, 'set_components', return_value=None), \
             patch.object(importer, 'product_cards', return_value=[dict(card, name='Набор картриджей')]), \
             patch.object(importer, 'warehouse_stock', return_value={card['id']: {'stock': 0, 'reserve': 0}}):
            result = importer.inspect({'number': source['number']}, {}, {}, 'digest')
        self.assertEqual(result['selected'][0]['card_code'], '4297zz')
        self.assertTrue(result['selected'][0]['manual_parent'])
        self.assertFalse(result['selected'][0]['available'])
