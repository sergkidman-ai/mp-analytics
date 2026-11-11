# поток: fin
import copy
import datetime as dt

from web import turnover_api as api


def assemble(today, sales=100, complete=True):
    rows = [('wb', [dict(account='wb_acc1', ym='2026-07', sales=sales, rets=0),
                    dict(account='wb_acc2', ym='2026-07', sales=sales, rets=0)]),
            ('ozon', [dict(account='oz_acc1', ym='2026-07', brand='Цифровой квадрат', sales=sales, rets=0, row_count=1),
                      dict(account='oz_acc2', ym='2026-07', brand='Dsquare', sales=sales, rets=0, row_count=1)]),
            ('yandex', [dict(account='ya_acc1', ym='2026-07', sales=sales, rets=0)] if complete else [])]
    coverage = [dict(account=acc, ym='2026-07', last_day='2026-07-31') for acc in ('wb_acc1','wb_acc2')]
    return api._assemble_royalty(today, rows, coverage)


def test_past_month_is_persisted_and_immune_to_later_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(api, '_ROYALTY_CLOSED_DIR', tmp_path)
    today = dt.date(2026, 8, 11)
    first = api._apply_royalty_closures(assemble(today), today, {})
    before = (tmp_path / '2026-07.json').read_bytes()
    saved = copy.deepcopy(first['months'][0])
    assert saved['status'] == 'closed'
    assert not saved['orgs']['7811803918']['royalty']['preliminary']
    again = api._apply_royalty_closures(assemble(today, sales=9999), today, api._closed_royalty_months())
    assert again['months'][0] == saved
    assert (tmp_path / '2026-07.json').read_bytes() == before


def test_current_month_is_always_estimated_and_not_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(api, '_ROYALTY_CLOSED_DIR', tmp_path)
    today = dt.date(2026, 7, 31)
    first = api._apply_royalty_closures(assemble(today), today, {})
    second = api._apply_royalty_closures(assemble(today, sales=200), today, {})
    assert first['months'][0]['status'] == 'calculated'
    assert first['months'][0]['orgs']['7811803918']['royalty']['preliminary']
    assert first['diskver_royalty_total']['amount'] != second['diskver_royalty_total']['amount']
    assert not list(tmp_path.glob('*.json'))


def test_past_month_with_missing_real_source_waits_for_final_report(tmp_path, monkeypatch):
    monkeypatch.setattr(api, '_ROYALTY_CLOSED_DIR', tmp_path)
    today = dt.date(2026, 8, 11)
    d = api._apply_royalty_closures(assemble(today, complete=False), today, {})
    assert d['months'][0]['status'] == 'awaiting_close'
    assert not list(tmp_path.glob('*.json'))


def test_existing_snapshot_cannot_be_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(api, '_ROYALTY_CLOSED_DIR', tmp_path)
    month = {'ym':'2026-07', 'status':'closed', 'value':123}
    api._save_closed_royalty(month)
    assert api._save_closed_royalty(dict(month,value=999)) == month


def test_api_queries_only_unfrozen_months(tmp_path, monkeypatch):
    monkeypatch.setattr(api, '_ROYALTY_CLOSED_DIR', tmp_path)
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 11, tzinfo=tz)
    monkeypatch.setattr(api.dt, 'datetime', Clock)
    july = assemble(dt.date(2026, 8, 11))['months'][0]
    july['status'] = 'closed'
    api._save_closed_royalty(july)
    seen = []
    def source(since):
        seen.append(since)
        return []
    for name in ('_wb','_royalty_ozon','_yandex'):
        monkeypatch.setattr(api, name, source)
    monkeypatch.setattr(api.db, 'query', lambda *a, **kw: [])
    monkeypatch.setattr(api, '_ROYALTY_CACHE', {})
    result = api.royalty_turnover()
    assert seen == [dt.date(2026,8,1)] * 3
    assert result['months'][0] == july
