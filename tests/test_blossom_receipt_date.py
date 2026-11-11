# поток: inv
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'invoice_bot'))
import invoice_to_po as invoices
import supplier_groups
import workcal
from core import db

class BlossomReceiptTests(unittest.TestCase):
    def test_all_legal_entities_and_organizations_use_next_workday(self):
        inns=supplier_groups.related_inns('9718075418')
        self.assertEqual(len(inns),4)
        with patch.object(db,'query',side_effect=AssertionError('legacy terms must not override Blossom')):
            for org in ['org-dsquare','org-digital',None]:
                for inn in inns:self.assertEqual(invoices.plan_skip(org,inn),0)

    def test_example_weekend_and_holiday(self):
        with patch.object(workcal,'isdayoff',side_effect=workcal._fallback), \
             patch.object(db,'query',side_effect=AssertionError('no DB needed for Blossom')):
            skip=invoices.plan_skip('org','9718075418')
            self.assertEqual(workcal.plan_date(date(2026,11,5),False,skip),date(2026,11,6))
            self.assertEqual(workcal.plan_date(date(2026,11,6),False,skip),date(2026,11,9))
            self.assertEqual(workcal.plan_date(date(2026,11,3),False,skip),date(2026,11,5))

    def test_colortek_keeps_two_workday_rule(self):
        with patch.object(db,'query',return_value=[{'d':2}]) as query, \
             patch.object(workcal,'isdayoff',side_effect=workcal._fallback):
            skip=invoices.plan_skip('org','7840480595')
            self.assertEqual(skip,1)
            self.assertEqual(workcal.plan_date(date(2026,11,5),False,skip),date(2026,11,9))
            query.assert_called_once()

    def test_other_suppliers_remain_configurable(self):
        with patch.object(db,'query',return_value=[{'d':3}]):
            self.assertEqual(invoices.plan_skip('org','7730244274'),2)
