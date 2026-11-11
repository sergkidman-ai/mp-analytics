# поток: inv
from copy import deepcopy
from decimal import Decimal
from datetime import datetime,timezone
from unittest import TestCase
from unittest.mock import patch
from ops.fbs_ms_bundle_quantity import bundle_multiplier,normalize_market
from ops.fbs_ms_order_payload import Line,line_total,build_payload
from ops import fbs_ms_import_next5 as imp


class BundleQuantityTests(TestCase):
    def market(self,code='6452X3MS22I0',count=1,price=386100,discount='0'):
        return {'lines':[{'market_code':code,'quantity':count,'price_kopeks':price,
                          'discount_percent':discount}]}

    def test_explicit_suffix_and_multiple_bundles_and_idempotence(self):
        for code,mult in [('6452X3MS22I0',3),('6452X6',6),('0042х12',12)]:
            before=self.market(code,count=2,price=120000);saved=deepcopy(before)
            after=normalize_market(before);item=after['lines'][0]
            self.assertEqual(item['quantity'],2*mult)
            self.assertEqual(line_total(Line('p',Decimal(item['quantity']),item['price_kopeks'])),240000)
            self.assertEqual(before,saved);self.assertEqual(normalize_market(after),after)

    def test_random_aliases_do_not_invent_bundle_quantity(self):
        self.assertEqual(bundle_multiplier('4692X5BV7567','Картриджи (4 шт)'),1)
        self.assertEqual(bundle_multiplier('1588X99JKQEZ','Картридж'),1)
        self.assertEqual(bundle_multiplier('3933X6HHWDS8','Комплект лент (6 шт)'),6)
        with self.assertRaisesRegex(ValueError,'card_title'):
            bundle_multiplier('3933X6HHWDS8')

    def test_fractional_unit_price_preserves_discounted_kopek_and_serializes(self):
        for price in [10001,386101,424301]:
            for discount in ['0','10.06','33.33']:
                old=self.market('6452X3',2,price,discount)['lines'][0]
                item=normalize_market({'lines':[old]})['lines'][0]
                expected=line_total(Line('p',Decimal(2),price,Decimal(discount)))
                line=Line('p',Decimal(item['quantity']),item['price_kopeks'],Decimal(discount))
                self.assertEqual(line_total(line),expected)
                payload=build_payload(platform='ozon',account='acc1',external_id='n',
                    lines=[line],expected_kopeks=expected,available=False,
                    now=datetime.now(timezone.utc))
                import json
                json.dumps(payload)

    def test_common_source_normalizes_before_selection(self):
        raw=dict(self.market(),platform='ozon',account='acc1')
        with patch.object(imp,'_source',return_value=raw):
            item=imp.source({}, {})['lines'][0]
        self.assertEqual(item['quantity'],3)
        self.assertEqual(item['price_kopeks'],128700)

    def test_stock_selection_requires_three_physical_units_not_one(self):
        from tests.test_fbs_ms_import_set_integration import product
        from ops.fbs_ms_order_payload import STORE
        from prices.profiles import STORE_REMOTE
        raw=dict(self.market('6452X3',price=300000),platform='ozon',account='acc1',
                 number='n',created_at='2026-11-06T09:00:00+03:00',status='awaiting_packaging')
        for physical,ready in [(2,False),(3,True)]:
            def stock(store,cards):
                return {c['id']:{'stock':physical if store==STORE['acc1'] else 10,'reserve':0} for c in cards}
            with patch.object(imp,'_source',return_value=raw), \
                 patch.object(imp,'product_cards',return_value=[product('6452')]), \
                 patch.object(imp,'find_customerorder',return_value=None), \
                 patch.object(imp,'set_components',return_value=None), \
                 patch.object(imp,'warehouse_stock',side_effect=stock):
                proof=imp.inspect({'number':'n'}, {}, {'id-6452':50000},'digest')
            self.assertEqual(proof['selected'][0]['available'],ready)
            self.assertEqual(proof['payload']['positions'][0]['quantity'],3)
            self.assertEqual(proof['payload']['positions'][0]['price'],100000)
            self.assertEqual(proof['total_kopeks'],300000)
            from ops.fbs_ms_order_payload import FIELDS
            flag=next(a for a in proof['payload']['attributes'] if a['meta']['href'].endswith(FIELDS['Бандл']))
            self.assertIs(flag['value'],True)

    def test_bundle_of_confirmed_sets_multiplies_each_component_once(self):
        import os
        from tests.test_fbs_ms_import_set_integration import product
        raw=dict(self.market('0168X3',price=600000),platform='ozon',account='acc1',
                 number='n',created_at='2026-11-06T09:00:00+03:00',status='awaiting_packaging')
        def stock(store,cards):
            return {c['id']:{'stock':0 if c['externalCode']=='0168' else 10,'reserve':0} for c in cards}
        with patch.dict(os.environ,{'FBS_SET_SPLIT_ENABLED':'1'}), \
             patch.object(imp,'_source',return_value=raw), \
             patch.object(imp,'product_cards',side_effect=lambda code:[product(code)]), \
             patch.object(imp,'find_customerorder',return_value=None), \
             patch.object(imp,'set_components',return_value=[('0161',1),('0162',1)]), \
             patch.object(imp,'warehouse_stock',side_effect=stock):
            proof=imp.inspect({'number':'n'}, {}, {'id-0161':50000,'id-0162':50000},'digest')
        from collections import Counter
        self.assertEqual(Counter(i['base_code'] for i in proof['selected']),{'0161':3,'0162':3})
        self.assertEqual(proof['total_kopeks'],600000)
