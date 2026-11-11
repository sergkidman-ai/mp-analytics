# поток: inv
import datetime as dt
from types import SimpleNamespace
import pytest
from web import opex_finance as f

ORG='7807355364'


def setup(monkeypatch,values,provisional=False):
    for platform,acc in [('wb','wb_acc1'),('ozon','oz_acc1'),('yandex','ya_acc1')]:
        h={'period_keys':['2026-09'],'accounts':{acc:{'net':[values[platform]]}},'provisional':['2026-09'] if provisional and platform=='yandex' else []}
        monkeypatch.setitem(f.MODULES,platform,SimpleNamespace(_hist=lambda h=h:h))


def test_saved_reports_provide_profit_without_recalculating_cogs(monkeypatch):
    setup(monkeypatch,{'wb':100,'ozon':200,'yandex':300},True)
    monkeypatch.setattr(f.mp_numbers,'business',lambda *a:pytest.fail('closed report recalculated'))
    d=f.business('2026-09-01',ORG,dt.date(2026,11,10))
    assert d['total']['net']==600 and d['provisional']==['yandex']
    assert set(d['sources'].values())=={'saved_report'}


def test_saved_zero_is_a_known_profit(monkeypatch):
    setup(monkeypatch,dict.fromkeys(['wb','ozon','yandex'],0))
    monkeypatch.setattr(f.mp_numbers,'business',lambda *a:pytest.fail('zero discarded'))
    assert f.business('2026-09',ORG,dt.date(2026,11,10))['total']['net']==0


def test_missing_one_required_platform_does_not_become_partial_profit(monkeypatch):
    setup(monkeypatch,{'wb':None,'ozon':200,'yandex':300})
    monkeypatch.setattr(f.mp_numbers,'business',lambda *a:{'platforms':{'wb':{'net':None}}})
    assert f.business('2026-09',ORG,dt.date(2026,11,10))['total']['net'] is None


def test_live_current_month_uses_one_shared_live_result(monkeypatch):
    calls=[]
    def live(*a):
        calls.append(a);return {'platforms':{p:{'net':1} for p in ['wb','ozon','yandex']}}
    monkeypatch.setattr(f.mp_numbers,'business',live)
    d=f.business('2026-11',ORG,dt.date(2026,11,10))
    assert d['total']['net']==3 and len(calls)==1
