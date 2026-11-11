from copy import deepcopy
from web.home_finance import complete_totals, enrich


def sample():
    return {'period':'2026-10-01','data_source':'saved_monthly_reports',
            'total':{'net':1000.,'revenue':5000.,'cogs':2000.,'margin_pct':20.},
            'wb':{'net':1000},'missing_accounts':[]}


def build(result=None,closed=True,coverage=None):
    def query(sql,args):
        return ([{'org_inn':'a'}] if closed else []) if 'opex_month_closure' in sql else (coverage or [])
    return enrich(result or sample(),'a',['a'],query,lambda *a:250,
                  lambda *a:{'n':2,'s':30},lambda *a:{'accounts':[],'missing':[]},lambda *a:None)


def test_restored_expense_and_profit():
    d=build()
    assert d['opex']==250 and d['net_after_opex']==750 and d['opex_closed']
    assert d['purchases']=={'net':None,'docs':None}
    assert d['opex_unassigned']=={'n':2,'s':30}
    assert 'предварительная' in d['recovery_note']


def test_missing_month_is_not_zero():
    d=build(closed=False)
    assert d['opex'] is None and d['net_after_opex'] is None


def test_missing_cabinet_is_not_complete_total():
    d=sample();d['missing_accounts']=['wb:wb_acc2'];before=deepcopy(d['total'])
    d=complete_totals(d,'')
    assert all(v is None for v in d['total'].values())
    assert d['known_subtotal']==before and d['wb'] is None


def test_no_profit_when_cogs_not_validated():
    d=sample();d['total']['net']=None
    assert build(d)['net_after_opex'] is None


def test_open_month_uses_available_statement():
    d=build(closed=False,coverage=[{'org_inn':'a','bank':'bank','first_day':'2026-01-01','last_day':'2026-10-31'}])
    assert d['opex']==250 and not d['opex_closed']
