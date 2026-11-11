# поток: fin
import datetime as dt
from decimal import Decimal

from web.home_weekly import aggregate, bounds


def event(date='2026-10-05',kind='sale',quantity=1,cost=100,reference='order1',account='wb_acc1',**values):
    return dict(date=dt.date.fromisoformat(date),account=account,reference=reference,kind=kind,
                products=[{'sku':'sku1','quantity':quantity,'unit_cost':cost,'cost_method':'shipment_fifo'}],
                **values)


def rows(events,start='2026-10-05',end='2026-10-11',accounts=('wb_acc1',)):
    return aggregate(events,dt.date.fromisoformat(start),dt.date.fromisoformat(end),accounts)


def test_full_weeks_cross_month_boundaries():
    assert bounds('2026-10-01') == (dt.date(2026,9,28),dt.date(2026,11,1))
    d=rows([event(date='2026-09-30',rev=100,payout=80),event(date='2026-11-01',rev=200,payout=150)],
           '2026-09-28','2026-11-01')
    assert [r['wk'] for r in d]==['2026-09-28','2026-10-26']


def test_promotion_is_already_in_settlement_and_is_not_subtracted_twice():
    d=rows([event(rev=1000,payout=700,ad=200)])[0]
    assert d['net']==600 and d['net_after_ad']==600 and d['ad']==200
    assert d['margin']==60


def test_return_uses_original_sale_cost_across_months_and_cost_changes():
    events=[event(date='2026-09-01',cost=100,rev=500,payout=400),
            event(date='2026-10-05',kind='return',cost=999,rev=-500,payout=-400)]
    d=rows(events)[0]
    assert d['cogs']==-100 and d['net']==-300
    assert d['cost_methods']=={'original_sale_return':1}


def test_partial_returns_cannot_reverse_more_than_was_charged():
    events=[event(date='2026-09-01',cost=100,quantity=2),
            event(date='2026-10-05',kind='return',quantity=1,payout=-500),
            event(date='2026-10-06',kind='return',quantity=2,payout=-500)]
    d=rows(events)[0]
    assert d['cogs']==-200 and d['storno_excess_units']==1


def test_missing_cost_is_not_zero_and_blocks_profit_for_whole_scope():
    d=rows([event(rev=500,payout=400),event(cost=None,reference='order2',rev=500,payout=400)])[0]
    assert d['rev']==1000 and d['net'] is None and d['margin'] is None
    assert d['known_cogs']==100 and d['cost_coverage_pct']==50
    assert d['missing_cost_units']==1


def test_unlinked_return_does_not_invent_cogs():
    d=rows([event(kind='return',cost=100,rev=-500,payout=-400)])[0]
    assert d['net'] is None and d['missing_cost_units']==1


def test_missing_transaction_composition_does_not_invent_one_unit():
    e=event(kind='return',payout=-500);e['products']=[];e['unverified_cost']=True
    d=rows([e])[0]
    assert d['net'] is None and d['unverified_cost_transactions']==1
    assert d['missing_cost_units']==0 and d['cost_gaps']==[]


def test_same_reference_and_sku_in_other_account_cannot_supply_return_cost():
    d=rows([event(date='2026-09-01',account='wb_acc2'),event(kind='return',rev=-500,payout=-400)])[0]
    assert d['net'] is None


def test_absent_account_is_not_a_complete_business_total():
    d=rows([event(rev=500,payout=400)],accounts=('wb_acc1','oz_acc1'))[0]
    assert d['net'] is None and d['missing_accounts']==['oz_acc1']


def test_fee_only_week_is_visible_even_without_revenue():
    fee=event(kind='fee',payout=-50,ad=-10);fee['products']=[]
    d=rows([fee])[0]
    assert d['rev']==0 and d['net']==-50 and d['ad']==-10
    assert d['margin'] is None


def test_dated_fifo_fallback_is_explicit_and_preserved_on_return():
    sale=event(date='2026-09-01',cost=Decimal('10.015'));sale['products'][0]['cost_method']='nabor_fifo'
    d=rows([sale,event(kind='return',payout=-50)])[0]
    assert d['cogs']==-10.02 and d['fallback_cost_units']==1


def test_ozon_distinct_places_keep_their_own_actual_costs(monkeypatch):
    from web import home_weekly as api
    from unittest.mock import Mock
    raw=[]
    for number,cost in [('123-001-1',100),('123-001-2',300)]:
        raw.append(dict(account='oz_acc1',date='2026-10-05',rid=number,op='OperationAgentDeliveredToCustomer',
                        revenue=500,amount=400,commission=-100,services=[],posting=number,
                        order_date='2026-10-01',skus=['sku1']))
    raw.append(dict(raw[1],date='2026-10-06',rid='return',op='OperationAgentStornoDeliveredToCustomer',
                    revenue=-500,amount=-400,commission=100))
    proofs=[dict(demand_name=number,org=api.ORG_IDS['1'],agent='Покупатель Озон',cogs=cost,qty=1,complete=True)
            for number,cost in [('123-001-1',100),('123-001-2',300)]]
    def query(sql,args=None):
        if 'FROM raw_ozon_transaction' in sql:return raw
        if 'FROM ms_demand_cogs' in sql:return proofs
        return []
    monkeypatch.setattr(api.db,'query',query)
    ff=Mock();ff.unit.side_effect=AssertionError('An exact shipment must precede historical fallback')
    monkeypatch.setattr(api.fifo_fallback,'load',lambda:ff)
    events=api._load(dt.date(2026,10,5),dt.date(2026,10,11),['oz_acc1'])
    d=rows(events,accounts=('oz_acc1',))[0]
    assert d['cogs']==100 and d['net']==300
    assert d['fallback_cost_units']==0
