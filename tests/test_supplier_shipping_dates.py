# поток: inv
import unittest
from datetime import datetime, timezone
from ops.supplier_shipping_dates import shortage_day, MSK
from ops.fbs_ms_post_import_status import desired

SPRINT='c16b41ef-3428-11ed-0a80-0e48002629ce'
SPRINT_MSK='9263b7de-97bf-11f0-0a80-015900374bc3'
EASY='daffd8b7-308f-11f0-0a80-05570004fd14'
BLOSSOM='141a0940-99f7-11e7-7a34-5acf0005e7dc'
COLORTEK='32425f47-88cf-11e7-6b01-4b1d000ab2f4'
OTHER='7225fcf1-10f7-11ea-0a80-06380004eeda'

class ShippingTests(unittest.TestCase):
    def test_holiday_cutoff_and_slowest_supplier(self):
        for supplier in [SPRINT,EASY,OTHER]:
            self.assertEqual(str(shortage_day('2026-11-03 16:00:01',[supplier])),'2026-11-06')
            self.assertEqual(str(shortage_day('2026-11-04 11:00:00',[supplier])),'2026-11-05')
        for supplier in [BLOSSOM,COLORTEK]:
            self.assertEqual(str(shortage_day('2026-11-03 16:00:01',[supplier])),'2026-11-09')
            self.assertEqual(str(shortage_day('2026-11-04 11:00:00',[supplier])),'2026-11-06')
        self.assertEqual(str(shortage_day('2026-11-03 16:00:01',[SPRINT,BLOSSOM])),'2026-11-09')

    def test_only_two_groups_work_saturday(self):
        for supplier in [SPRINT,EASY]:
            self.assertEqual(str(shortage_day('2026-11-06 14:00:00',[supplier])),'2026-11-07')
            self.assertEqual(str(shortage_day('2026-11-06 15:00:01',[supplier])),'2026-11-09')
        for supplier in [OTHER,SPRINT_MSK,None]:
            self.assertEqual(str(shortage_day('2026-11-06 14:00:00',[supplier])),'2026-11-09')
        self.assertEqual(str(shortage_day('2026-11-06 14:00:00',[COLORTEK])),'2026-11-10')

    def test_cutoff_uses_moscow_and_creation_not_processing(self):
        self.assertEqual(str(shortage_day('2026-11-03T12:00:00Z',[OTHER])),'2026-11-05')
        self.assertEqual(str(shortage_day('2026-11-03T12:00:01Z',[OTHER])),'2026-11-06')
        from tests.test_fbs_ms_post_import_status import fixture
        order,proof,_=fixture(('a','b'));order['created']='2026-11-03 16:00:01'
        proof['selected'][1].update(available=False,purchase_kopeks=43000,tier=2,supplier_id=OTHER)
        got=desired(order,proof,{}, {},datetime(2026,11,10,10,tzinfo=MSK))
        self.assertEqual(got[:2],('Неукомплектован','2026-11-06'))
