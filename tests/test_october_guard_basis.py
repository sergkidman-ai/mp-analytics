# поток: fin
"""Finalized reports, signed credits and the fixed Market rule."""
from datetime import date
from decimal import Decimal
import pytest
from core import mp_guard_basis as policy
from core import ozon_expense_basis as oz


def closed_report():
    lines = {k: [0] for k in ('sales', 'returns', *oz.EXPENSE_LINES, 'compensation', 'other')}
    for k,v in {'sales':1000,'returns':100,'commission':300,'delivery':100,
                'unclassified':-20,'other':40,'compensation':10}.items(): lines[k] = [v]
    return dict(period_keys=['2026-10'],provisional=[],
                accounts={'oz_acc1':dict(lines=lines,cogs=[200],net=[370])})


def test_closed_basis_credits_returns_and_profit_threshold():
    b=oz.closed_snapshot_basis(closed_report(),'oz_acc1','2026-10','frozen',as_of=date(2026,11,9))
    assert b['expenses']=='380' and b['sales']=='1000'
    assert b['retention']=='0.38' and b['keep']=='0.62'
    assert b['version']=='ozexp-v1:'+oz.digest({k:v for k,v in b.items() if k!='version'})
    assert oz.floor_price(150,1,b)==Decimal('564.52')
    assert oz.floor_price(151,1,b)==Decimal('727.42')


@pytest.mark.parametrize('failure',['provisional','missing','open','net_mismatch'])
def test_unconfirmed_report_rejected(failure):
    s=closed_report();today=date(2026,11,9)
    if failure=='provisional':s['provisional']=['2026-10']
    if failure=='missing':s['period_keys']=[]
    if failure=='open':today=date(2026,10,31)
    if failure=='net_mismatch':s['accounts']['oz_acc1']['net']=[999]
    with pytest.raises(oz.BasisUnavailable):
        oz.closed_snapshot_basis(s,'oz_acc1','2026-10','frozen',as_of=today)


def test_guard_rejects_wrong_published_month(monkeypatch):
    b=oz.closed_snapshot_basis(closed_report(),'oz_acc1','2026-10','frozen')
    monkeypatch.setattr(oz,'get_basis',lambda *a,**kw:b)
    assert policy.ozon_basis('oz_acc1') is b
    b['period_from']='2026-09-01'
    with pytest.raises(oz.BasisUnavailable,match='approved_ozon_guard_month_missing'):
        policy.ozon_basis('oz_acc1')


def test_existing_wb_consumers_read_october():
    from ops import wb_promo_guard as guard
    from ops import wb_auto_entry as entry
    from ops import wb_plan_raise as plan
    rules=policy.wb_rules()
    assert set(rules)=={'wb_acc1','wb_acc2'}
    for account,r in rules.items():
        assert r['base_from']==date(2026,10,1) and r['base_to']==date(2026,10,31)
        assert guard.RETENTION[account]==float(r['retention'])
    assert entry.guard is guard and plan.w is guard


def test_quarantine_and_market_keep_fixed_rate(monkeypatch):
    from ops import price_quarantine as q
    from ops import ya_promo_guard as market
    monkeypatch.setattr(policy,'ozon_basis',lambda account:{'retention':'0.6'})
    rates,month=q.retention()
    assert month=='2026-10' and rates['ya_acc1']==.72
    assert q.yandex_retention()[0]==market.GUARD_RETENTION==Decimal('.72')
    assert set(q.ya_retention_bands())=={.72}
    for price in (100,999,1000,10000,100000):
        assert q.ya_rate(q.ya_retention_bands(),.11,price)==.72


def test_scheduled_refresh_preserves_explicit_closed_basis(monkeypatch,tmp_path):
    import json
    from core import db
    b=oz.closed_snapshot_basis(closed_report(),'oz_acc1','2026-10','frozen')
    monkeypatch.setattr(db,'query',lambda *a,**kw:[{'payload':b}])
    monkeypatch.setattr(oz,'build',lambda *a,**kw:pytest.fail('raw must not replace finalized reports'))
    folder=tmp_path/'reports/data';folder.mkdir(parents=True)
    (folder/'mp_ozon_hist.json').write_text(json.dumps(closed_report()))
    monkeypatch.setattr(oz,'__file__',str(tmp_path/'core/ozon_expense_basis.py'))
    result=oz.refresh('oz_acc1',as_of=date(2026,12,1))
    assert result['period_from']=='2026-10-01' and result['expenses']=='380'
    assert result['version']==b['version']
    changed=closed_report();changed['accounts']['oz_acc1']['lines']['delivery']=[101]
    changed['accounts']['oz_acc1']['net']=[369]
    (folder/'mp_ozon_hist.json').write_text(json.dumps(changed))
    with pytest.raises(oz.BasisUnavailable,match='activated_closed_report_changed'):
        oz.refresh('oz_acc1',as_of=date(2026,12,1))
