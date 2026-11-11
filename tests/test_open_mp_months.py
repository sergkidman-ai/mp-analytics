# поток: fin
import datetime as dt
from unittest import TestCase
from reports.open_months import select_open_month


class OpenMonthTests(TestCase):
    def test_new_month_is_selectable_while_previous_is_still_open(self):
        self.assertEqual(select_open_month('2026-11',['2026-09'],today=dt.date(2026,11,6)),(2026,11))
        self.assertEqual(select_open_month('2026-10-01',['2026-09'],today=dt.date(2026,11,6)),(2026,10))

    def test_closed_or_future_period_cannot_be_recomputed(self):
        for value in ('2026-09','2026-08','2026-12','2026-13','2026-11-06','anything'):
            with self.subTest(value=value):
                self.assertIsNone(select_open_month(value,['2026-09'],today=dt.date(2026,11,6)))

    def test_year_boundary_and_missing_history(self):
        self.assertEqual(select_open_month('2027-01',['2026-12'],today=dt.date(2027,1,2)),(2027,1))
        self.assertIsNone(select_open_month('2027-01',[],today=dt.date(2027,1,2)))
