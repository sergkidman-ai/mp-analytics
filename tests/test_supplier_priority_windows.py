# поток: inv
from datetime import datetime, timezone
from decimal import Decimal
from unittest import TestCase
from ops.ms_supplier_priorities import (supplier_priority, priority_time, POLICY_VERSION,
    LEGACY_POLICY_VERSION, selection_time)
from ops.fbs_ms_import_policy import Card, choose_card, choose_bundle_plan
from ops.fbs_ms_post_import_status import supplier_selection_proof, provenance
from prices.profiles import STORE_REMOTE
from invoice_bot.supplier_groups import GROUPS


class WindowTests(TestCase):
    def test_all_legal_entities_and_exact_boundaries(self):
        cases = [('2026-11-05T15:59:59+03:00', None),
                 ('2026-11-05T16:00:00+03:00', (15,20,15)),
                 ('2026-11-06T15:59:59+03:00', (15,20,15)),
                 ('2026-11-06T16:00:00+03:00', (20,15,20)),
                 ('2026-11-08T16:59:59+03:00', (20,15,20)),
                 ('2026-11-08T17:00:00+03:00', None)]
        from ops.ms_supplier_priorities import base_supplier_priority
        for stamp, values in cases:
            for index, group in enumerate(('Спринт МСК','Одиссей','Изипринт')):
                for _, _, uid in GROUPS[group]:
                    with self.subTest(stamp=stamp, group=group, uid=uid):
                        expected = values[index] if values else base_supplier_priority(uid)
                        self.assertEqual(supplier_priority(uid,as_of=stamp),expected)

    def test_utc_and_repeated_weeks_and_naive_time(self):
        uid=GROUPS['Одиссей'][0][2]
        for day in ('2026-11-06','2026-11-13'):
            self.assertEqual(supplier_priority(uid,as_of=day+'T13:00:00+00:00'),15)
        with self.assertRaises(ValueError):
            supplier_priority(uid,as_of=datetime(2026,11,6,16))

    def test_same_window_for_nested_calls_and_context_restored(self):
        with priority_time('2026-11-06T15:59:59+03:00') as before:
            with priority_time():
                self.assertEqual(selection_time(),before)
            with priority_time('2026-11-07T12:00:00+03:00'):
                self.assertNotEqual(selection_time(),before)
            self.assertEqual(selection_time(),before)

    def test_stock_first_and_strict_premium_and_bundle_use_same_window(self):
        od=Card('od','1234',GROUPS['Одиссей'][0][2],'o',120000,Decimal(10))
        iz=Card('iz','1234',GROUPS['Изипринт'][0][2],'i',100000,Decimal(10))
        from dataclasses import replace
        own=[replace(od,stock=Decimal(0)),replace(iz,stock=Decimal(0))]
        with priority_time('2026-11-07T12:00:00+03:00'):
            self.assertEqual(choose_card(own, '1234',1,[od,iz]),(od,False))
            self.assertEqual(choose_bundle_plan([('1234',Decimal(1))],[],[od,iz])[0][0],od)
            self.assertEqual(choose_card([iz],'1234',1,[od,iz]),(iz,True))
            self.assertEqual(choose_card(own,'1234',1,[replace(od,purchase_kopeks=120001),iz]),(iz,False))

    def test_historical_proof_is_not_reinterpreted_after_window_switch(self):
        uid=GROUPS['Одиссей'][0][2]
        item={'purchase_kopeks':10000,'selection_policy':POLICY_VERSION,'priority':20,
              'priority_at':'2026-11-06T15:59:59+03:00','supplier_id':uid,
              'stock_store_id':STORE_REMOTE,'quantity':'1','supplier_available':'1',
              'available':False,'card_id':'p'}
        with priority_time('2026-11-07T12:00:00+03:00'):
            self.assertTrue(supplier_selection_proof(item))
            self.assertTrue(supplier_selection_proof(dict(item,selection_policy=LEGACY_POLICY_VERSION)))
            self.assertFalse(supplier_selection_proof(dict(item,priority_at=None)))
            self.assertFalse(supplier_selection_proof(dict(item,priority_at='bad')))
            self.assertEqual(provenance({'selected':[item,item]}),(False,False))
