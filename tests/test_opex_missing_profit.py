# поток: inv
"""Expense facts remain usable while MP financial history is incomplete."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('net,expected',[(None,None),(0,-100),(500,400)])
def test_expenses_do_not_require_profit_and_zero_stays_known(net,expected):
    tree=ast.parse(Path('web/app.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='opex')
    fn.decorator_list=[]
    scope={
        '_opex_month':lambda p:p+'-01','_opex_is_fact':lambda p:True,
        'ORG_NAMES':{'7807355364':'Test'},'ORG_ORDER':['7807355364'],
        'ORG_ACCOUNTS':{'7807355364':['wb_acc1','oz_acc1']},
        '_mpn':SimpleNamespace(business=lambda *a:{'platforms':{'wb':{'net':net},'ozon':{'net':0}},'total':{'net':net}}),
        'db':SimpleNamespace(query=lambda *a,**kw:[{'category_id':1,'category':'ФОТ','amount':100,'txn_count':1}]),
        '_opex_unassigned':lambda *a:{'n':0,'s':0},
    }
    scope['_of']=scope['_mpn']
    scope['_oc']=SimpleNamespace(summary=lambda *a:None)
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'web/app.py','exec'),scope)
    result=scope['opex']('2026-01','7807355364')
    assert result['source']=='fact' and result['total']==100
    assert result['biz_net']==net and result['wb_net']==net
    assert result['net_after']==expected
    assert result['financials_complete']==(net is not None)
