# поток: fin
from decimal import Decimal as D
import pytest

# Load candidate modules before legacy ops imports prepend the shared checkout.
from core.profit_floor import minimum_profit, POLICY_VERSION
from core.ozon_expense_basis import floor_from_keep
from collectors.yandex_finance_basis import floor_price as ya_floor
from ops import wb_promo_guard as wb, ozon_promo_guard as oz, ya_promo_guard as ya
from ops import ozon_stock_action as ladder


@pytest.mark.parametrize('cost,qty,profit', [
    ('0.01',1,200), ('149.99',1,200), ('150',1,200), ('150.01',1,300),
    ('900',6,1200), ('900.01',6,1800), ('6000',6,1800), ('40000',6,4000),
])
def test_exact_boundary_quantity_and_unchanged_high_cost(cost,qty,profit):
    assert minimum_profit(cost,qty)==D(profit)
    assert wb.min_net(float(cost),qty)==profit
    expected=((D(cost)+profit)/D('.4')).quantize(D('.01'))
    assert floor_from_keep(cost,qty,'.4')==expected
    assert D(str(ladder.floor_price(float(cost),D('.4'),qty)))==expected
    if qty==1:
        assert ya_floor(cost,'.4').quantize(D('.01'))==expected


@pytest.mark.parametrize('cost,qty', [('NaN',1),('Infinity',1),(-1,1),(0,1),(100,0),(100,'NaN')])
def test_bad_cost_is_not_a_cheap_product(cost,qty):
    with pytest.raises(ValueError):minimum_profit(cost,qty)


@pytest.mark.parametrize('account', ['wb_acc1','wb_acc2'])
def test_wb_keeps_cheap_order_that_old_policy_would_raise(account):
    row=wb.decide(account,{1:(2000,60,800,'0001')},{1:{'cogs':100,'bundle':1}},
                  {1:1000},.6)[0]
    assert row['floor_price']==750
    assert row['status']!='todo'


@pytest.mark.parametrize('account', ['oz_acc1','oz_acc2'])
def test_ozon_keeps_cheap_product_in_action(account):
    row=dict(account=account,offer_id='0001',cogs=100,cogs_source='supplier_min',
             keep_ratio=D('.4'),action_price=800,max_price=1000)
    oz.decide(row)
    assert row['floor_price']==750 and row['mode']=='ok'


def test_yandex_guard_uses_lower_floor_and_keeps_missing_price_safety():
    row=dict(offer_id='0001',cogs=100,keep_ratio=D('.4'),promo_price=800,price=2000,
             max_promo_price=1000,mechanics='DIRECT_DISCOUNT')
    assert ya.decide(row)['mode']=='ok' and row['floor_price']==750
    assert ya.decide(dict(row,promo_price=None))['mode']=='frozen'
