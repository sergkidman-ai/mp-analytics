# поток: mkt
from contextlib import nullcontext
import json
from unittest.mock import Mock

from ops import wb_plan_raise_report as r
from ops import wb_plan_raise as p


def prepare(monkeypatch, tmp_path):
    monkeypatch.setattr(r, 'REPORTS', tmp_path)
    monkeypatch.setattr(r, 'plan_lock', nullcontext)
    monkeypatch.setattr(r, 'legacy_main', lambda fn: fn())
    monkeypatch.setattr(r, 'recipient', lambda: 123)
    send = Mock(return_value={'message_id': 456})
    monkeypatch.setattr(r, 'api', send)
    monkeypatch.setattr(r.importer, 'auto', Mock())
    return send


def test_zero_actions_still_sends_one_report(monkeypatch, tmp_path):
    send = prepare(monkeypatch, tmp_path)
    def calculate(report, argv):
        assert argv == ['--apply']
        report['accounts']['wb_acc1'] = dict(candidates=0, daily_skipped=0, selected=0,
                                            confirmed=0, unconfirmed=0, gain=0)
        return 0
    monkeypatch.setattr(r.optimizer, 'main', calculate)
    assert r.execute(apply=True) == 0
    send.assert_called_once()
    assert send.call_args.args[1]['chat_id'] == 123
    assert 'Кандидатов: 0' in send.call_args.args[1]['text']
    saved = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert saved['telegram_message_id'] == 456


def test_import_failure_reports_and_never_runs_prices(monkeypatch, tmp_path):
    send = prepare(monkeypatch, tmp_path)
    r.importer.auto.side_effect = RuntimeError('secret request URL')
    calculate = Mock()
    monkeypatch.setattr(r.optimizer, 'main', calculate)
    assert r.execute(apply=True) == 1
    calculate.assert_not_called()
    text = send.call_args.args[1]['text']
    assert 'импорт выгрузок' in text
    assert 'secret' not in text


def test_notification_failure_keeps_successful_price_result(monkeypatch, tmp_path):
    send = prepare(monkeypatch, tmp_path)
    send.side_effect = RuntimeError('secret URL')
    monkeypatch.setattr(r.optimizer, 'main', lambda *a, **k: 0)
    assert r.execute(apply=True) == 1
    saved = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert saved['exit_code'] == 0
    assert saved['notification_error'] == 'RuntimeError'
    assert 'secret' not in json.dumps(saved)


def test_dry_does_not_import_notify_or_apply(monkeypatch, tmp_path):
    send = prepare(monkeypatch, tmp_path)
    calculate = Mock(return_value=0)
    monkeypatch.setattr(r.optimizer, 'main', calculate)
    assert r.execute() == 0
    assert calculate.call_args.kwargs['argv'] == []
    send.assert_not_called()
    r.importer.auto.assert_not_called()
    assert not list(tmp_path.glob('*.json'))


def test_busy_import_lock_reports_deferral_without_running_prices(monkeypatch, tmp_path):
    send = prepare(monkeypatch, tmp_path)
    lock = Mock(side_effect=BlockingIOError)
    monkeypatch.setattr(r, 'plan_lock', lock)
    calculate = Mock()
    monkeypatch.setattr(r.optimizer, 'main', calculate)
    assert r.execute(apply=True) == 75
    calculate.assert_not_called()
    r.importer.auto.assert_not_called()
    assert 'Запуск отложен' in send.call_args.args[1]['text']


def test_partial_batch_failure_is_not_reported_as_success(monkeypatch):
    rows = [dict(nm_id=n, gain=10, cap=100, disc_before=40, send_disc=30,
                 status='dry', err=None) for n in (1, 2, 3)]
    monkeypatch.setattr(p, 'BATCH', 1)
    monkeypatch.setattr(p, 'verify_calendar_coverage', lambda *a: None)
    monkeypatch.setattr(p, 'candidates', lambda *a: rows)
    monkeypatch.setattr(p.db, 'query', lambda *a: [])
    monkeypatch.setattr(p.w, 'push', Mock(side_effect=[(True, ''), (False, 'timeout')]))
    monkeypatch.setattr(p.w, 'save', Mock())
    report = {}
    assert p.main(report, argv=['--account', 'wb_acc1', '--apply']) == 1
    stats = report['accounts']['wb_acc1']
    assert stats['selected'] == 3
    assert stats['confirmed'] == stats['unconfirmed'] == 1
    assert stats['gain'] == 10
    report.update(exit_code=1, started_msk='test')
    assert 'Не отправлено: 1' in r.message(report)
