# поток: prc
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from prices import cabinet_schedule as schedule
from prices import mail_schedule
from ops import prc_price_watch as watcher


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=schedule.MSK)


def loaded(value, **extra):
    return {"id": 1416, "moment": at(value), **extra}


@pytest.mark.parametrize("now,slot", [
    ("2026-10-19T07:00", "2026-10-18T18:17"),
    ("2026-10-19T08:07", "2026-10-18T18:17"),
    ("2026-10-19T10:06:59", "2026-10-18T18:17"),
    ("2026-10-19T10:07", "2026-10-19T08:07"),
    ("2026-10-19T13:13", "2026-10-19T08:07"),
    ("2026-10-19T15:12", "2026-10-19T13:12"),
    ("2026-10-19T20:17", "2026-10-19T18:17"),
    ("2026-10-19T23:59", "2026-10-19T18:17"),
    ("2026-10-20T00:00", "2026-10-19T18:17"),
    ("2026-10-17T10:07", "2026-10-17T08:07"),  # Saturday
    ("2026-10-18T15:12", "2026-10-18T13:12"),  # Sunday
])
def test_completion_deadlines_every_day_and_midnight(now, slot):
    assert schedule.expected_slot(at(now)) == at(slot)


def test_utc_clock_is_compared_in_moscow():
    now = datetime.fromisoformat("2026-10-19T07:07:00+00:00")
    assert schedule.expected_slot(now) == at("2026-10-19T08:07")


@pytest.mark.parametrize("now,last", [
    ("2026-10-19T09:30", "2026-10-18T18:18"),  # Current run still has time.
    ("2026-10-19T10:50", "2026-10-19T10:43"),  # Manual start of cabinet loader.
    ("2026-10-19T14:00", "2026-10-19T08:10"),
    ("2026-10-19T15:13", "2026-10-19T13:12"),
])
def test_successful_cabinet_load_satisfies_its_slot(now, last):
    assert schedule.evaluate(at(now), loaded(last)) is None


@pytest.mark.parametrize("now,last,slot", [
    ("2026-10-19T10:07", "2026-10-18T18:18", "2026-10-19T08:07"),
    ("2026-10-19T13:20", "2026-10-18T18:18", "2026-10-19T08:07"),
    ("2026-10-19T15:12", "2026-10-19T10:43", "2026-10-19T13:12"),
    ("2026-10-19T20:17", "2026-10-19T13:20", "2026-10-19T18:17"),
])
def test_failed_or_missed_run_alerts_without_waiting_for_mail(now, last, slot):
    assert schedule.evaluate(at(now), loaded(last))["slot"] == at(slot)


def test_completing_old_plan_today_does_not_cover_today_schedule():
    last = loaded("2026-10-18T18:18", created_at=at("2026-10-19T10:08"))
    assert schedule.evaluate(at("2026-10-19T10:09"), last) is not None


def test_no_successful_history_is_overdue():
    assert schedule.evaluate(at("2026-10-19T10:30"), None)["last_load"] is None


def test_only_completed_real_cabinet_enters_count(monkeypatch):
    # Exercise the real selection against isolated data, not production history.
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE prc_price_load (id INTEGER, supplier_key TEXT,
                   source_kind TEXT, status TEXT, dry_run BOOLEAN, docs INTEGER,
                   rows_loaded INTEGER, moment TEXT, created_at TEXT)""")
        rows = [
            (1, "s_print_msk", "cabinet", "ok", False, 9, 864, "10:43"),
            (2, "s_print_msk", "unprocessed", "ok", False, 9, 864, "10:59"),
            (3, "s_print_msk", "file", "ok", False, 9, 864, "10:58"),
            (4, "s_print_msk", "cabinet", "ok", True, 9, 864, "10:57"),
            (5, "s_print_msk", "cabinet", "error", False, 9, 864, "10:56"),
            (6, "s_print_msk", "cabinet", "ok", False, 0, 864, "10:55"),
            (7, "s_print_msk", "cabinet", "ok", False, 9, 0, "10:54"),
            (8, "colortek", "cabinet", "ok", False, 9, 864, "10:53"),
            (9, "s_print_msk", "cabinet", "ok", False, 9, 864, "23:00"),
        ]
        db.executemany("INSERT INTO prc_price_load VALUES (?,?,?,?,?,?,?,?,?)",
                       [(*r[:-1], at("2026-10-19T" + r[-1]).isoformat(),
                         at("2026-10-19T10:59").isoformat()) for r in rows])

        def query(sql, params):
            values = tuple(v.isoformat() if isinstance(v, datetime) else v for v in params)
            result = [dict(r) for r in db.execute(sql.replace("%s", "?"), values)]
            for r in result:
                r["moment"] = datetime.fromisoformat(r["moment"])
            return result

        monkeypatch.setattr(schedule, "query", query)
        assert schedule.overdue("s_print_msk", at("2026-10-19T11:00")) is None
        late = schedule.overdue("s_print_msk", at("2026-10-19T15:30"))
        assert late["last_load"]["id"] == 1
        db.execute("DELETE FROM prc_price_load WHERE id=1")
        assert schedule.overdue("s_print_msk", at("2026-10-19T11:00"))["last_load"] is None


def test_sprint_never_uses_old_mail_history(monkeypatch):
    monkeypatch.setattr(mail_schedule, "schedule", Mock(side_effect=AssertionError("legacy mail")))
    assert mail_schedule.overdue("s_print_msk") is None


def test_schedule_and_grace_match_loader_units():
    root = Path(__file__).resolve().parents[1]
    timer = (root / "prices/prc-solutionsprint.timer").read_text()
    actual = [line.split(" ")[-1] for line in timer.splitlines() if line.startswith("OnCalendar=")]
    now = at("2026-10-19T00:00")
    expected = [(datetime.combine(now.date(), t, schedule.MSK) - timedelta(hours=3)).strftime("%H:%M:%S")
                for t in schedule.RUN_TIMES]
    assert actual == expected
    assert "TimeoutStartSec=2h" in (root / "prices/prc-solutionsprint.service").read_text()
    assert schedule.COMPLETION_GRACE == timedelta(hours=2)


@pytest.fixture
def monitoring(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "LOG_DIR", tmp_path)
    monkeypatch.setattr(watcher, "NOTIFY_IDS", ["owner"])
    monkeypatch.setattr(watcher, "NOVELTY_IDS", ["operator", "owner"])
    current = {"late": schedule.evaluate(at("2026-10-19T10:30"), loaded("2026-10-18T18:18"))}
    monkeypatch.setattr(schedule, "overdue", lambda key: current["late"])
    send = Mock(return_value="owner: ok; operator: ok")
    monkeypatch.setattr(watcher, "tg", send)
    for name in ("fetch_latest_price", "run_load", "done_today", "note_arrival", "read_state"):
        monkeypatch.setattr(watcher, name, Mock(side_effect=AssertionError("mail path: " + name)))
    return current, send, tmp_path


def test_sprint_main_monitors_cabinet_without_any_mail_or_loader(monitoring):
    _, send, tmp = monitoring
    assert watcher.main(["--supplier", "s_print_msk", "--force"]) == 0
    assert send.call_count == 1
    text, chats = send.call_args.args
    assert "не обновлены оприходования из ЛК" in text
    assert "08:07 / 13:12 / 18:17" in text
    assert "прайса сегодня нет" not in text
    assert chats == ["owner", "operator"]
    assert list(tmp.glob("*_cabinet.json"))


@pytest.mark.parametrize("mode", ["--dry", "--check-overdue"])
def test_read_only_modes_do_not_notify_or_change_locks(monitoring, mode, capsys):
    _, send, tmp = monitoring
    watcher.main(["--supplier", "s_print_msk", mode])
    assert "не обновлены оприходования из ЛК" in capsys.readouterr().out
    send.assert_not_called()
    assert not list(tmp.iterdir())


def test_quiet_does_not_consume_notification_slot(monitoring):
    _, send, tmp = monitoring
    watcher.main(["--supplier", "s_print_msk", "--quiet"])
    send.assert_not_called()
    assert not list(tmp.glob("*_cabinet.json"))
    watcher.main(["--supplier", "s_print_msk"])
    send.assert_called_once()


def test_one_alert_per_missed_slot_ignores_legacy_daily_lock(monitoring):
    current, send, tmp = monitoring
    (tmp / "prc_price_watch_s_print_msk_overdue.txt").write_text("2026-10-19")
    watcher.main(["--supplier", "s_print_msk"])
    watcher.main(["--supplier", "s_print_msk"])
    assert send.call_count == 1
    current["late"] = schedule.evaluate(at("2026-10-19T15:30"), loaded("2026-10-18T18:18"))
    watcher.main(["--supplier", "s_print_msk"])
    assert send.call_count == 2


def test_failed_telegram_delivery_is_retried_without_duplicate_successes(monitoring):
    _, send, _ = monitoring
    send.return_value = "owner: ok; operator: ConnectionError offline"
    watcher.main(["--supplier", "s_print_msk"])
    send.return_value = "operator: ok"
    watcher.main(["--supplier", "s_print_msk"])
    assert send.call_args.args[1] == ["operator"]
    watcher.main(["--supplier", "s_print_msk"])
    assert send.call_count == 2


def test_successful_refresh_is_silent_and_next_missed_slot_alerts(monitoring):
    current, send, _ = monitoring
    current["late"] = None
    watcher.main(["--supplier", "s_print_msk"])
    send.assert_not_called()
    current["late"] = schedule.evaluate(at("2026-10-19T15:30"), loaded("2026-10-19T10:43"))
    watcher.main(["--supplier", "s_print_msk"])
    send.assert_called_once()


def test_other_suppliers_keep_mail_monitoring(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "LOG_DIR", tmp_path)
    check = Mock(return_value=None)
    monkeypatch.setattr(watcher.mail_schedule, "overdue", check)
    monkeypatch.setattr(watcher.cabinet_schedule, "overdue", Mock(side_effect=AssertionError("cabinet")))
    assert watcher.main(["--supplier", "colortek", "--check-overdue"]) == 0
    check.assert_called_once_with("colortek")
