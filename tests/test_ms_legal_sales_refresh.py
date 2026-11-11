# поток: fin
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import pytest
from collectors import ms_legal_sales as sales
from collectors.mp_cogs_common import MsReadError


@pytest.mark.parametrize('today,expected',[
 (date(2026,11,10),('2026-10','2026-11')),
 (date(2026,11,30),('2026-10','2026-11')),
 (date(2027,1,3),('2026-12','2027-01')),
 (date(2026,1,10),('2026-01',))])
def test_refresh_keeps_previous_month_after_day_five_and_handles_year_boundary(today,expected):
    assert sales.refresh_months(today)==expected


def test_refresh_scans_both_months_and_rechecks_their_fifo(monkeypatch,tmp_path):
    monkeypatch.setattr(sales,'__file__',str(tmp_path/'collectors/ms_legal_sales.py'))
    calls=[];reader=object()
    monkeypatch.setattr(sales,'scan',lambda month,**kwargs:calls.append((month,kwargs)))
    def build(**kwargs):
        assert kwargs==dict(reader=reader,refresh_cost_months={'2026-10','2026-11'},refresh_agents=True)
        return 7
    monkeypatch.setattr(sales,'build',build)
    assert sales.refresh(date(2026,11,10),reader)==7
    assert [m for m,_ in calls]==['2026-10','2026-11']
    assert all(k==dict(reader=reader,through=date(2026,11,10)) for _,k in calls)


def test_overlapping_refresh_is_rejected_and_lock_is_released(monkeypatch,tmp_path):
    monkeypatch.setattr(sales,'__file__',str(tmp_path/'collectors/ms_legal_sales.py'))
    with sales.refresh_lock():
        with pytest.raises(MsReadError,match='already running'):
            with sales.refresh_lock():pass
    with sales.refresh_lock():pass


def test_scan_failure_preserves_published_report(monkeypatch,tmp_path):
    monkeypatch.setattr(sales,'__file__',str(tmp_path/'collectors/ms_legal_sales.py'))
    report=tmp_path/'report.json';report.write_bytes(b'previous verified report')
    monkeypatch.setattr(sales,'OUT',report)
    def fail(*a,**kw):raise MsReadError('upstream incomplete')
    monkeypatch.setattr(sales,'scan',fail)
    monkeypatch.setattr(sales,'build',lambda **kw:pytest.fail('must not publish partial data'))
    with pytest.raises(MsReadError,match='upstream incomplete'):sales.refresh(date(2026,11,10),object())
    assert report.read_bytes()==b'previous verified report'
