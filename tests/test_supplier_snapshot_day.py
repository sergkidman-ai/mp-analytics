# поток: mkt
import datetime
from unittest import TestCase

from collectors.suppliers import snapshot_day


class SupplierSnapshotDayTests(TestCase):
    def test_after_moscow_midnight_uses_new_day(self):
        now = datetime.datetime(2026, 11, 2, 21, 10, tzinfo=datetime.timezone.utc)
        self.assertEqual(snapshot_day(now), '2026-11-03')
