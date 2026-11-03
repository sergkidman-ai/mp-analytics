# поток: inv
from dataclasses import replace
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from ops import fbs_ms_import_next5 as importer
from ops.fbs_ms_import_policy import Card, SelectionError, choose_card, choose_bundle_plan
from ops.fbs_ms_post_import_status import provenance
from ops.ms_supplier_priorities import POLICY_VERSION, supplier_priority
from prices.profiles import STORE_REMOTE

T2 = 'daffd8b7-308f-11f0-0a80-05570004fd14'
SK = '2c7ed80c-c44f-11eb-0a80-08be0030bb17'
CS = '0b14dc16-c51e-11ef-0a80-0c79000af987'


def card(uid, supplier, price, stock=1, code='0060'):
    return Card(uid, code, supplier, uid, price, Decimal(stock))


class SupplierPriorityTests(TestCase):
    def choose(self, *offers, quantity=1):
        own = [replace(c, stock=Decimal(0)) for c in offers]
        return choose_card(own, '0060', quantity, list(offers))[0]

    def test_twenty_percent_boundary(self):
        for high_price, expected in [(12000, 't2'), (12001, 'cs')]:
            with self.subTest(price=high_price):
                self.assertEqual(self.choose(card('t2', T2, high_price),
                                             card('cs', CS, 10000)).id, expected)

    def test_same_priority_cheapest_and_stable_tie(self):
        self.assertEqual(self.choose(card('a', SK, 10000), card('b', SK, 10000),
                                     card('c', SK, 11000)).id, 'a')

    def test_intermediate_priority_within_twenty_percent_is_kept(self):
        self.assertEqual(self.choose(card('t2', T2, 129233), card('sk', SK, 70000),
                                     card('cs', CS, 63200)).id, 'sk')

    def test_unknown_price_or_insufficient_supplier_stock_is_excluded(self):
        self.assertEqual(self.choose(card('t2', T2, 129233, 93),
                                     card('sk', SK, 40000, 0), card('cs', CS, None, 500)).id, 't2')
        self.assertEqual(self.choose(card('t2', T2, 129233, 93),
                                     card('cs', CS, 63200, 1), quantity=2).id, 't2')
        with self.assertRaises(SelectionError):
            self.choose(card('t2', T2, 129233, 0))

    def test_additional_table_suppliers_are_eligible_by_id(self):
        self.assertEqual(supplier_priority('9ccfa04f-0c0d-11e8-7a69-8f55000fe09f'), 40)
        self.assertEqual(supplier_priority('dd6f9218-ec82-11e6-7a69-97110027a1f0'), 10)
        self.assertEqual(supplier_priority('c16b41ef-3428-11ed-0a80-0e48002629ce'), 40)
        self.assertEqual(supplier_priority('59ae3427-7e0e-11ee-0a80-04960008d1f8'), 40)
        self.assertIsNone(supplier_priority('unknown'))

    def test_bundle_total_purchase_premium_respects_own_stock_and_pairs(self):
        for t2_price, expected in [(12000, T2), (12001, CS)]:
            remote = [card('t2', T2, t2_price, 10), card('cs', CS, 10000, 10)]
            plan = choose_bundle_plan([('0060', 2)], [], remote)
            self.assertEqual({c.supplier_id for c, _, _ in plan}, {expected})
        own = [card('sk', SK, None, 2)]
        self.assertTrue(all(available for _, _, available in choose_bundle_plan([('0060', 2)], own, remote)))

    def test_status_accepts_new_proof_and_never_falls_back_on_invalid_new_policy(self):
        item = {'available': False, 'selection_policy': POLICY_VERSION, 'priority': 30,
                'supplier_id': CS, 'purchase_kopeks': 63200, 'supplier_available': '500',
                'quantity': '1', 'stock_store_id': STORE_REMOTE, 'tier': 2, 'card_id': 'cs'}
        self.assertEqual(provenance({'selected': [item]}), (False, True))
        for changes in [{'priority': 15}, {'supplier_available': '0'}, {'supplier_available': 'NaN'},
                        {'selection_policy': 'wrong'}, {'stock_store_id': None}]:
            with self.subTest(changes=changes):
                self.assertEqual(provenance({'selected': [dict(item, **changes)]}), (False, False))

    def test_repeated_components_cannot_exceed_supplier_stock_in_status_proof(self):
        item = {'available': False, 'selection_policy': POLICY_VERSION, 'priority': 30,
                'supplier_id': CS, 'purchase_kopeks': 63200, 'supplier_available': '1',
                'quantity': '1', 'stock_store_id': STORE_REMOTE, 'card_id': 'cs'}
        self.assertEqual(provenance({'selected': [item, dict(item)]}), (False, False))
        item['supplier_available'] = '2'
        self.assertEqual(provenance({'selected': [item, dict(item)]}), (False, True))

    def test_disputed_order_full_import_payload_and_remote_promises(self):
        domain = [card('t2', T2, 129233, 93), card('sk', SK, None, 0), card('cs', CS, 63200, 500)]
        cards = [{'id': c.id, 'code': '0060' + c.id, 'externalCode': '0060', 'name': 'Картридж',
                  'meta': {'type': 'product', 'href': 'https://example/entity/product/' + c.id},
                  'supplier': {'meta': {'href': 'https://example/entity/counterparty/' + c.supplier_id}}}
                 for c in domain]
        source = {'platform': 'ozon', 'account': 'acc1', 'number': '59453200-0650-1',
                  'created_at': '2026-11-03T09:16:19Z', 'status': 'awaiting_packaging',
                  'lines': [{'market_code': '0060', 'quantity': 1,
                             'price_kopeks': 392100, 'discount_percent': '15.43'}]}
        def stock(store, _):
            return {c.id: {'stock': float(c.stock) if store == STORE_REMOTE else 0, 'reserve': 0}
                    for c in domain}
        with patch.object(importer, 'source', return_value=source), \
             patch.object(importer, 'find_customerorder', return_value=None), \
             patch.object(importer, 'set_components', return_value=None), \
             patch.object(importer, 'product_cards', return_value=cards), \
             patch.object(importer, 'warehouse_stock', side_effect=stock):
            result = importer.inspect({'number': source['number']}, {},
                                      {c.id: c.purchase_kopeks for c in domain if c.purchase_kopeks}, 'digest')
            exhausted_cs = importer.inspect({'number': source['number']}, {},
                {c.id: c.purchase_kopeks for c in domain if c.purchase_kopeks}, 'digest',
                {(STORE_REMOTE, 'cs'): Decimal(500)})
            self.assertEqual(exhausted_cs['selected'][0]['card_code'], '0060t2')
        selected = result['selected'][0]
        self.assertEqual(selected['card_code'], '0060cs')
        self.assertEqual(selected['priority'], 30)
        self.assertEqual(selected['supplier_available'], '500.0')
        self.assertEqual(result['total_kopeks'], 331599)
        self.assertEqual(provenance(result), (False, True))
        self.assertEqual(importer._unreserved_promises(result['selected'], [], 'own'),
                         {(STORE_REMOTE, 'cs'): Decimal(1)})
