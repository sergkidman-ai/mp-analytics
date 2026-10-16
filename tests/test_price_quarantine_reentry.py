"""Поток mkt: повторный карантин, свежий запрет, возобновление и нулевая запись."""
import copy
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops import price_quarantine as q


def target(status="ВЫПУЩЕН", price="500"):
    return {"account": "wb_acc1", "id": "123", "target": price,
            "status": status, "updated": "2026-09-01"}


def row(verdict="ОК", price=400):
    return {"platform": "wb", "account": "wb_acc1", "id": 123,
            "verdict": verdict, "price_new": price}


class QuarantineReentry(unittest.TestCase):
    def test_reentry_reopens_completed_episode_with_new_price(self):
        tg = {("wb_acc1", "123"): target()}
        work = q.targets_refresh(tg, [row(price=400)], "2026-10-03")
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["target"], "400")
        self.assertEqual(work[0]["status"], "в работе")

    def test_same_price_reentry_is_rechecked(self):
        tg = {("wb_acc1", "123"): target()}
        self.assertEqual(len(q.targets_refresh(tg, [row(price=500)], "2026-10-03")), 1)

    def test_fresh_stop_blocks_old_pending_and_stays_paused(self):
        for verdict in ("СТОП", "НЕТ СЕБЕСТА", "НЕТ ЦЕНЫ"):
            with self.subTest(verdict=verdict):
                tg = {("wb_acc1", "123"): target("НЕ ДОШЁЛ")}
                self.assertEqual(q.targets_refresh(tg, [row(verdict)], "2026-10-03"), [])
                self.assertEqual(q.targets_refresh(tg, [], "2026-10-04"), [])
                work = q.targets_refresh(tg, [row(price=600)], "2026-10-04")
                self.assertEqual(work[0]["target"], "600")

    def test_pending_ladder_survives_disappearance_from_quarantine(self):
        tg = {("wb_acc1", "123"): target("НЕ ДОШЁЛ")}
        work = q.targets_refresh(tg, [], "2026-10-03")
        self.assertEqual(work, [target("НЕ ДОШЁЛ")])

    def test_other_account_and_market_do_not_reopen_target(self):
        tg = {("wb_acc1", "123"): target()}
        other = row(); other["account"] = "wb_acc2"
        market = row(); market["platform"] = "ya"
        work = q.targets_refresh(tg, [other, market], "2026-10-03")
        self.assertEqual([r["account"] for r in work], ["wb_acc2"])
        self.assertEqual(tg[("wb_acc1", "123")]["status"], "ВЫПУЩЕН")

    def test_repeated_reentry_at_actual_target_uploads_no_prices(self):
        tg = {("wb_acc1", "123"): target()}
        with patch.object(q, "targets_load", side_effect=lambda: copy.deepcopy(tg)), \
                patch.object(q, "targets_save", side_effect=lambda value: tg.update(copy.deepcopy(value))), \
                patch.object(q, "wb_current", return_value=(500, 0, 500)), \
                patch.object(q, "wb_push") as upload:
            for _ in range(2):
                result = q.apply_ok([row(price=500)])
                self.assertEqual(result[-1]["result"], "ВЫПУЩЕН")
                self.assertIn("шагов 0", result[-1]["detail"])
        upload.assert_not_called()

    def test_reentry_uses_current_goal_and_saves_entire_queue_before_release(self):
        tg = {("wb_acc1", "123"): target()}
        calls = []
        with patch.object(q, "targets_load", return_value=tg), \
                patch.object(q, "targets_save", side_effect=lambda value: calls.append(("save", copy.deepcopy(value)))), \
                patch.object(q, "wb_release", side_effect=lambda *args: (calls.append(("release", args[2])) or ("ВЫПУЩЕН", 1, args[2]))):
            q.apply_ok([row(price=400)])
        self.assertEqual(calls[0][0], "save")
        self.assertEqual(calls[0][1][("wb_acc1", "123")]["status"], "в работе")
        self.assertEqual(calls[1], ("release", 400.0))

    def test_reentry_actually_sends_current_target_not_historical_one(self):
        tg = {("wb_acc1", "123"): target(price="500")}
        with patch.object(q, "targets_load", return_value=tg), \
                patch.object(q, "targets_save"), \
                patch.object(q, "wb_current", side_effect=[(500, 0, 500), (400, 0, 400)]), \
                patch.object(q, "wb_push", return_value=(True, "")) as upload:
            result = q.apply_ok([row(price=400)])
        upload.assert_called_once_with("wb_acc1", "123", 400)
        self.assertEqual(result[-1]["result"], "ВЫПУЩЕН")

    def test_fresh_stop_never_calls_price_release(self):
        with patch.object(q, "targets_load", return_value={("wb_acc1", "123"): target("НЕ ДОШЁЛ")}), \
                patch.object(q, "targets_save"), patch.object(q, "wb_release") as release:
            q.apply_ok([row("СТОП")])
        release.assert_not_called()

    def test_atomic_journal_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(q, "TARGETS", Path(folder) / "targets.csv"):
            tg = {("wb_acc1", "123"): target()}
            q.targets_save(tg)
            self.assertEqual(q.targets_load(), tg)
            self.assertFalse(q.TARGETS.with_suffix(".tmp").exists())

    def test_failed_save_keeps_previous_journal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(q, "TARGETS", Path(folder) / "targets.csv"):
            tg = {("wb_acc1", "123"): target()}
            q.targets_save(tg)
            broken = {("wb_acc1", "123"): {**target("в работе"), "unexpected": "bad column"}}
            with self.assertRaises(ValueError):
                q.targets_save(broken)
            self.assertEqual(q.targets_load(), tg)

    def test_empty_queue_dry_run_writes_valid_report(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(q, "BASE_DIR", Path(folder)), \
                patch.object(q, "build", return_value=([], "Сен")), \
                patch("sys.argv", ["price_quarantine.py"]):
            q.main_run()
            reports = list((Path(folder) / "docs/reports").glob("*.csv"))
            self.assertEqual(len(reports), 1)
            with reports[0].open() as f:
                self.assertIn("price_new", next(csv.reader(f)))

    def test_parallel_run_does_not_start_second_worker(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(q, "BASE_DIR", Path(folder)), \
                patch.object(q.fcntl, "flock", side_effect=BlockingIOError), \
                patch.object(q, "main_run") as run:
            q.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
