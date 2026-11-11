# поток: fin
from decimal import Decimal
from unittest.mock import Mock
import pytest
from ops.customer_order_fast_drain import FastCustomerMS
from shipment_guard.config import MS
from shipment_guard.ms import SourceError


def test_fast_positions_read_complete_receipt_in_one_request():
    client = FastCustomerMS(session=Mock())
    rows = [{'id': str(i)} for i in range(100)]
    client.get = Mock(return_value={'meta': {'size': 100}, 'rows': rows})
    assert client.positions('enter', 'one') == rows
    assert client.get.call_count == 1
    assert client.get.call_args.args[1]['limit'] == 100


def test_incomplete_pagination_never_becomes_full_evidence():
    client = FastCustomerMS(session=Mock())
    client.get = Mock(side_effect=[{'meta': {'size': 101}, 'rows': [{}] * 100},
                                   {'meta': {'size': 101}, 'rows': []}])
    with pytest.raises(SourceError, match='ms_collection_changed'):
        client.positions('enter', 'one')


def test_receipt_cache_reuses_same_version_and_refetches_changed_version():
    FastCustomerMS.receipt_cache.clear()
    version = ['v1'];position_reads=[]
    product=MS+'/entity/product/p'
    def get(path, params=None):
        if path == '/entity/product/p':
            return {'article':'article','supplier':{'meta':{'href':MS+'/entity/counterparty/s'}}}
        receipt = {'id':'e','updated':version[0],'rate':{'currency':{'code':'643'}}}
        if path == '/entity/enter':
            return {'meta':{'size':1},'rows':[receipt]}
        if path == '/entity/enter/e':
            return receipt
        if path == '/entity/enter/e/positions':
            position_reads.append(version[0])
            return {'meta':{'size':1},'rows':[{'id':'ep','assortment':{'meta':{'href':product}},
                                                'price':100 if version[0]=='v1' else 300}]}
        raise AssertionError(path)
    for expected, current in [(Decimal(1),'v1'),(Decimal(1),'v1'),(Decimal(3),'v2')]:
        version[0]=current
        client=FastCustomerMS(session=Mock());client.remote=MS+'/entity/store/remote';client.get=get
        assert client.supplier_cost([{'assortment':{'meta':{'href':product}},'quantity':1}])[0] == expected
    assert position_reads == ['v1','v2']
    FastCustomerMS.receipt_cache.clear()


def test_transient_identity_read_retries_without_abandoning_cohort(monkeypatch):
    from datetime import datetime, timezone
    from ops import customer_order_fast_drain as fast
    now = datetime.now(timezone.utc)
    def query(sql, params=None):
        if 'last_tick_at' in sql:
            return [{'last_tick_at': now}]
        return [{'enabled':True,'notifications_enabled':True,'settings':{}}]
    monkeypatch.setattr(fast.db,'query',query)
    monkeypatch.setattr(fast,'due_ids',lambda now:[1])
    monkeypatch.setattr(fast,'remaining',lambda ids,started:0)
    monkeypatch.setattr(fast,'due_count',lambda:0)
    monkeypatch.setattr(fast,'metric',lambda name:0)
    monkeypatch.setattr(fast,'active_state',lambda:'inactive')
    systemctl=Mock();monkeypatch.setattr(fast,'run_systemctl',systemctl)
    monkeypatch.setattr(fast,'FastCustomerMS',Mock())
    worker=Mock(side_effect=[SourceError('ms_network_error',5),{'completed':1,'ms_requests':5}])
    monkeypatch.setattr(fast.service,'work',worker)
    sleep=Mock();monkeypatch.setattr(fast.time,'sleep',sleep)
    fast.main()
    assert worker.call_count==2 and sleep.call_args.args==(5,)
    assert systemctl.call_args.args==('start',fast.TIMER)
