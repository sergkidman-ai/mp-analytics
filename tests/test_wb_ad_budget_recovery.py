# поток: mkt
"""Budget recovery: missing statistics cannot justify transfers or a healthy report."""
import datetime as dt
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from ops import wb_ad_budget_guard as guard


def prepare_scan(monkeypatch, stats):
    monkeypatch.setattr(guard, 'composition', lambda account: {7: {'payment': 'cpc', 'status': 9}})
    monkeypatch.setattr(guard.db, 'query', lambda *args: stats)
    monkeypatch.setattr(guard, 'read_budgets', lambda account, ids: {7: 9})


def test_missing_stats_are_unknown_not_zero_orders(monkeypatch):
    prepare_scan(monkeypatch, [])
    row = guard.scan('wb_acc1')[0]
    assert row['statistics_state'] == 'MISSING'
    assert row['bal'] == 9 and row['low']
    assert row['o28'] is None and row['sp28'] is None
    assert row['topup'] is None and not row['good']


def test_original_recommendation_requires_current_stats(monkeypatch):
    yesterday = dt.datetime.now(ZoneInfo('Europe/Moscow')).date() - dt.timedelta(days=1)
    prepare_scan(monkeypatch, [{'advert_id': 7, 'last_day': yesterday,
                               'sp7': 140, 'sp28': 350, 'o28': 3, 'source_rows': 28}])
    row = guard.scan('wb_acc1')[0]
    assert row['statistics_state'] == 'CURRENT'
    assert row['good'] and row['low'] and row['topup'] == 1000


def test_stale_stats_cannot_recommend_topup(monkeypatch):
    old = dt.datetime.now(ZoneInfo('Europe/Moscow')).date() - dt.timedelta(days=2)
    prepare_scan(monkeypatch, [{'advert_id': 7, 'last_day': old,
                               'sp7': 140, 'sp28': 350, 'o28': 3, 'source_rows': 28}])
    row = guard.scan('wb_acc1')[0]
    assert row['statistics_state'] == 'STALE' and row['topup'] is None


def test_degraded_dry_run_never_sends_or_reports_healthy(monkeypatch, tmp_path):
    prepare_scan(monkeypatch, [])
    monkeypatch.setattr(guard, 'REPORT', tmp_path / 'report.txt')
    monkeypatch.setattr(guard, 'STATUS', tmp_path / 'status.json')
    send = Mock()
    monkeypatch.setattr(guard, 'send_sergey', send)
    monkeypatch.setattr('sys.argv', ['wb_ad_budget_guard', '--dry'])
    guard.main()
    send.assert_not_called()
    saved = json.loads(guard.STATUS.read_text())
    assert saved['state'] == 'DEGRADED' and saved['recommended_topup_total_RUB'] is None
    assert saved['automatic_deposits_enabled'] is False
    assert 'Выгодные кампании при деньгах' not in guard.REPORT.read_text()


def test_budget_batches_only_use_read_endpoint(monkeypatch, tmp_path):
    def post(url, headers, json, timeout):
        assert url.endswith('/api/advert/v2/budget')
        assert len(json['advertIds']) <= 50
        r = Mock(status_code=200)
        r.json.return_value = {'adverts': [{'advertId': i, 'currency': 'RUB', 'total': 1}
                                          for i in json['advertIds']]}
        return r
    request = Mock(side_effect=post)
    monkeypatch.setattr(guard.requests, 'post', request)
    monkeypatch.setattr(guard, '_token', lambda account: 'test-token')
    monkeypatch.setattr(guard, 'REPORT', tmp_path / 'report.txt')
    monkeypatch.setattr(guard.time, 'sleep', lambda seconds: None)
    result = guard.read_budgets('wb_acc1', list(range(51)))
    assert len(result) == 51 and request.call_count == 2
    raw = list((tmp_path / 'wb_ad_budget_guard_raw/wb_acc1').glob('*.json'))
    assert len(raw) == 2
    assert sum(len(json.loads(f.read_text())['response']['adverts']) for f in raw) == 51
