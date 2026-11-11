# поток: cf
import datetime as dt

import pytest

from cf import mp_forecast as forecast


@pytest.fixture(autouse=True)
def no_confirmed_schedule(monkeypatch):
    monkeypatch.setattr(forecast, '_ozon_lk_schedule', lambda *args: [])


def accruals():
    start = dt.date(2026, 10, 5)
    return [dict(account=acc, w=start + dt.timedelta(weeks=i),
                 s=(i + 1) * scale, last_day=start + dt.timedelta(weeks=i, days=6))
            for acc, scale in [('oz_acc1', 100), ('oz_acc2', 10)] for i in range(5)]


def test_both_accounts_use_four_week_lag_through_december_2(monkeypatch):
    monkeypatch.setattr(forecast.db, 'query', lambda *args: accruals())
    lines = forecast.ozon_forecast(dt.date(2026, 11, 10), dt.date(2026, 12, 2))
    dates = [dt.date(2026, 11, 11) + dt.timedelta(weeks=i) for i in range(4)]
    for org, scale in [('7807355364', 100), ('7811803918', 10)]:
        actual = [r for r in lines if r['org_inn'] == org]
        assert [r['day'] for r in actual] == dates
        assert [r['amount'] for r in actual] == [2 * scale, 3 * scale, 4 * scale, 5 * scale]
        assert all(not r['estimate'] for r in actual)
        assert all('отсрочка 4 недели' in r['note'] for r in actual)
        # The already paid Oct 5 week is not scheduled again.
        assert all(not r['ref'].endswith('2026-10-05') for r in actual)


@pytest.mark.parametrize('today,count', [(dt.date(2026, 11, 11), 2),
                                       (dt.date(2026, 11, 12), 0)])
def test_payment_day_included_but_past_payments_not_repeated(monkeypatch, today, count):
    monkeypatch.setattr(forecast.db, 'query', lambda *args: accruals())
    assert len(forecast.ozon_forecast(today, dt.date(2026, 11, 11))) == count


def test_unfinished_week_remains_an_estimate_at_four_week_lag(monkeypatch):
    rows = accruals() + [dict(account='oz_acc1', w=dt.date(2026, 11, 9),
                              s=70, last_day=dt.date(2026, 11, 10))]
    monkeypatch.setattr(forecast.db, 'query', lambda *args: rows)
    lines = forecast.ozon_forecast(dt.date(2026, 11, 10), dt.date(2026, 12, 9))
    last = next(r for r in lines if r['org_inn'] == '7807355364' and r['day'] == dt.date(2026, 12, 9))
    assert last['estimate'] is True
    assert last['amount'] == round(70 + ((200 + 300 + 400 + 500) / 4) / 7 * 5, 2)
    assert last['ref'].endswith('2026-11-09')


def test_lk_overrides_only_its_account_date_and_preserves_components(monkeypatch):
    monkeypatch.setattr(forecast.db, 'query', lambda *args: accruals())
    monkeypatch.setattr(forecast, '_ozon_lk_schedule', lambda *args: [
        dict(account='oz_acc1', payment_date=dt.date(2026, 11, 25),
             amount=390, components=['350', '40'])])
    lines = forecast.ozon_forecast(dt.date(2026, 11, 10), dt.date(2026, 12, 2))
    same_day = [r for r in lines if r['day'] == dt.date(2026, 11, 25)]
    assert len(lines) == 8 and len(same_day) == 2
    ck = next(r for r in same_day if r['org_inn'] == '7807355364')
    ds = next(r for r in same_day if r['org_inn'] == '7811803918')
    assert ck['amount'] == 390 and ck['source'] == 'ozon_lk_schedule'
    assert '350.00 + 40.00' in ck['note'] and not ck['estimate']
    assert ds['amount'] == 40 and ds['source'] == 'mp_forecast'


def test_confirmed_zero_suppresses_fallback_and_schedule_works_without_accruals(monkeypatch):
    monkeypatch.setattr(forecast.db, 'query', lambda *args: accruals())
    confirmed = [dict(account='oz_acc1', payment_date=dt.date(2026, 11, 11),
                      amount=0, components=[])]
    monkeypatch.setattr(forecast, '_ozon_lk_schedule', lambda *args: confirmed)
    lines = forecast.ozon_forecast(dt.date(2026, 11, 10), dt.date(2026, 11, 11))
    assert len(lines) == 1 and lines[0]['org_inn'] == '7811803918'
    monkeypatch.setattr(forecast.db, 'query', lambda *args: [])
    confirmed[0]['amount'] = 120
    lines = forecast.ozon_forecast(dt.date(2026, 11, 10), dt.date(2026, 11, 11))
    assert len(lines) == 1 and lines[0]['amount'] == 120
