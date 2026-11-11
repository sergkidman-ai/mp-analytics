# поток: mkt
"""A moving WB catalog is reread, never merged or followed by a blind price POST."""
from unittest.mock import Mock
import pytest
from ops.wb_auto_entry import Client
from ops import wb_promo_guard

def item(nm):
 return dict(nmID=nm,discount=20,vendorCode='0001',currencyIsoCode4217='RUB',sizes=[dict(price=1000,discountedPrice=800)])
def data(rows):return {'data':{'listGoods':rows}}

def client(monkeypatch):
 monkeypatch.setattr(wb_promo_guard,'head',lambda account:{})
 c=Client('wb_acc1',budget=120);c.raw=Mock()
 monkeypatch.setattr('ops.wb_auto_entry.time.sleep',lambda *args:None)
 return c

def test_duplicate_catalog_is_restarted_from_zero(monkeypatch):
 c=client(monkeypatch);first=[item(n) for n in range(1,1001)]
 c.get=Mock(side_effect=[data(first),data([item(1)]),data(first),data([item(1001)])])
 c.request=Mock(side_effect=AssertionError('No price POST is permitted'))
 goods,invalid=c.goods()
 assert len(goods)==1001 and not invalid
 assert [call.args[1]['offset'] for call in c.get.call_args_list]==[0,1000,0,1000]
 c.request.assert_not_called()

def test_repeat_instability_remains_a_failure(monkeypatch):
 c=client(monkeypatch);first=[item(n) for n in range(1,1001)]
 c.get=Mock(side_effect=[data(first),data([item(1)])]*3)
 with pytest.raises(RuntimeError,match='goods pagination duplicate'):c.goods()
 assert c.get.call_count==6

def test_point_response_errors_are_not_hidden(monkeypatch):
 c=client(monkeypatch);c.get=Mock(return_value=data([item(1),item(1)]))
 with pytest.raises(RuntimeError,match='goods pagination duplicate'):c.goods(1)
 assert c.get.call_count==1
