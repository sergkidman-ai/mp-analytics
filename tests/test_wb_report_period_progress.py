# поток: fin
"""A weekly report gap must not invent future days in a completed month."""
import datetime as dt
import unittest

from reports.wb_mp_report import _period_progress


class ReportPeriodProgress(unittest.TestCase):
    def test_october_with_last_monday_report_is_finished_in_november(self):
        self.assertEqual(_period_progress(2026, 10, dt.date(2026, 10, 26),
                                          dt.date(2026, 11, 8)), (31, 0, True))

    def test_current_month_keeps_forecast_after_last_weekly_report(self):
        self.assertEqual(_period_progress(2026, 11, dt.date(2026, 11, 2),
                                          dt.date(2026, 11, 8)), (2, 28, False))

    def test_last_calendar_day_has_not_finished_yet(self):
        self.assertEqual(_period_progress(2026, 10, dt.date(2026, 10, 26),
                                          dt.date(2026, 10, 31)), (26, 5, False))

    def test_missing_account_data_is_not_claimed_to_cover_the_month(self):
        self.assertEqual(_period_progress(2026, 10, None, dt.date(2026, 11, 8)),
                         (0, 0, True))


if __name__ == '__main__':
    unittest.main()
