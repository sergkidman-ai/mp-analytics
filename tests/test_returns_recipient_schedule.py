# поток: ret
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from returns_bot import tg
class RecipientScheduleTests(unittest.TestCase):
    def test_saturday_excludes_only_configured_user_from_both_daily_reports(self):
        config={'TG_RETURNS_ALLOWED_IDS':'full,office,other','TG_RETURNS_NOTIFY_ID':'full',
                'TG_RETURNS_LIST_ONLY_IDS':'office,other','TG_RETURNS_NO_SATURDAY_IDS':'office'}
        with patch.dict(os.environ,config):
            for hour in (6,11):
                self.assertEqual(tg.daily_notify_ids(datetime(2026,10,31,hour,tzinfo=timezone.utc)),['full','other'])
            self.assertEqual(tg.daily_notify_ids(datetime(2026,10,30,11,tzinfo=timezone.utc)),['full','office','other'])
            self.assertEqual(tg.daily_notify_ids(datetime(2026,11,2,11,tzinfo=timezone.utc)),['full','office','other'])
            self.assertIn('office',tg.allowed_ids())
            self.assertFalse(tg.can_receive_codes('office'))
    def test_saturday_is_determined_in_moscow_not_utc(self):
        with patch.object(tg,'notify_ids',return_value=['full']),patch.object(tg,'list_only_ids',return_value={'office'}),patch.dict(os.environ,{'TG_RETURNS_NO_SATURDAY_IDS':'office'}):
            self.assertEqual(tg.daily_notify_ids(datetime(2026,10,30,22,tzinfo=timezone.utc)),['full'])
if __name__=='__main__':unittest.main()
