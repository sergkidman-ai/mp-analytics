# поток: infra
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock
import gzip,json
import pytest
from core import ms_enter_cache as cache
from customer_guard.sources import CustomerMS
from shipment_guard.config import MS
from shipment_guard.ms import SourceError
from ops import fbs_ms_import_wb_pilot as importer
from collectors import suppliers

D1='11111111-1111-1111-1111-111111111111';D2='22222222-2222-2222-2222-222222222222'
P1='33333333-3333-3333-3333-333333333333'


def receipt(identity=D1,version='v1'):
    return dict(id=identity,updated=version,rate={'currency':{'code':'643'}})


def positions(price=100):
    return dict(meta={'size':1},rows=[dict(id='position1',assortment={'meta':{'href':MS+'/entity/product/'+P1}},price=price,vat=5,vatEnabled=True)])


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(cache,'ROOT',tmp_path)


def test_exact_document_version_only_and_corruption_is_a_miss():
    doc=receipt();page=positions()
    assert cache.write(doc,page) and cache.read(doc)==page
    assert cache.read(receipt(version='v2')) is None
    assert cache.read(receipt(identity=D2)) is None
    path=next(cache.ROOT.rglob('*.gz'));path.write_bytes(b'broken')
    assert cache.read(doc) is None


@pytest.mark.parametrize('bad',['partial','duplicate','missing_id'])
def test_partial_or_ambiguous_positions_are_never_cached(bad):
    page=positions()
    if bad=='partial':page['meta']['size']=2
    if bad=='duplicate':page['rows']*=2;page['meta']['size']=2
    if bad=='missing_id':del page['rows'][0]['id']
    assert not cache.write(receipt(),page)
    assert cache.read(receipt()) is None


def test_modified_payload_fails_integrity_check():
    cache.write(receipt(),positions());path=next(cache.ROOT.rglob('*.gz'))
    with gzip.open(path,'rt') as stream:data=json.load(stream)
    data['page']['rows'][0]['price']=99999
    with gzip.open(path,'wt') as stream:json.dump(data,stream)
    assert cache.read(receipt()) is None


def test_failed_cache_write_does_not_fail_valid_source(monkeypatch):
    monkeypatch.setattr(cache,'ROOT',cache.ROOT/'file')
    cache.ROOT.write_text('not directory')
    assert cache.write(receipt(),positions()) is False


def customer(version='v1',price=100,changed_after=False):
    client=CustomerMS(session=Mock());client.remote=MS+'/entity/store/remote'
    client.collection=Mock(return_value=[receipt(version=version)])
    reads=[]
    def get(path,params=None):
        reads.append(path)
        if path=='/entity/product/'+P1:return dict(article='0012',supplier={'meta':{'href':MS+'/entity/counterparty/s'}})
        if path=='/entity/enter/'+D1:return receipt(version='changed' if changed_after else version)
        raise AssertionError(path)
    client.get=get;client.positions=Mock(return_value=positions(price)['rows'])
    return client,reads


def selected():
    return [dict(assortment={'meta':{'href':MS+'/entity/product/'+P1}},quantity=2)]


def test_customer_cache_skips_only_positions_and_rechecks_live_version():
    first,reads=customer();cost,proof=first.supplier_cost(selected())
    assert cost==Decimal(2) and first.positions.call_count==1
    second,reads=customer();cost2,proof2=second.supplier_cost(selected())
    assert cost2==cost and proof2==proof and second.positions.call_count==0
    assert reads==['/entity/product/'+P1,'/entity/enter/'+D1]
    changed,_=customer(version='v2',price=300)
    assert changed.supplier_cost(selected())[0]==Decimal(6)
    assert changed.positions.call_count==1


def test_live_version_change_rejects_even_cached_positions():
    cache.write(receipt(),positions())
    client,_=customer(changed_after=True)
    with pytest.raises(SourceError,match='supplier_receipt_changed'):client.supplier_cost(selected())
    assert not client.receipts


def test_importer_reloads_only_changed_supplier_document(monkeypatch):
    versions=['v1','v1'];reads=[]
    def docs():return [('a','today',receipt(D1,versions[0])),('b','today',receipt(D2,versions[1]))]
    def get(path,params):reads.append(path);return positions(100 if D1 in path else 200)
    monkeypatch.setattr(importer,'current_purchase_documents',docs)
    monkeypatch.setattr(importer.ms_api,'get',get)
    _,first=importer._purchase_positions_snapshot();assert len(reads)==2
    reads.clear();_,second=importer._purchase_positions_snapshot();assert not reads and first==second
    versions[1]='v2';_,third=importer._purchase_positions_snapshot()
    assert reads==['/entity/enter/'+D2+'/positions']
    assert third[(D1,'v1')]==first[(D1,'v1')]


def test_importer_changed_generation_does_not_publish_old_positions(monkeypatch):
    iterations=iter([[('a','today',receipt(D1,'v1'))],[('a','today',receipt(D2,'v2'))],[('a','today',receipt(D2,'v2'))]])
    monkeypatch.setattr(importer,'current_purchase_documents',lambda:next(iterations))
    monkeypatch.setattr(importer.ms_api,'get',lambda path,params:positions())
    docs,pages=importer._purchase_positions_snapshot()
    assert list(pages)==[(D2,'v2')]
    assert cache.read(receipt(D1,'v1')) is None
    assert cache.read(receipt(D2,'v2')) is not None


def test_light_night_snapshot_uses_current_catalogue_price_not_old_price(monkeypatch):
    def query(sql):
        if 'FROM supplier_stock' in sql:return [dict(ms_id='p',supplier='Supplier',buy_price=1)]
        return [dict(ms_id='p',buy_price=10),dict(ms_id='new',buy_price=20),dict(ms_id='zero',buy_price=0)]
    monkeypatch.setattr(suppliers.db,'query',query)
    assert suppliers.last_supplier_map()=={'p':('Supplier',10.0),'new':(None,20.0),'zero':(None,None)}


def test_empty_store_clears_prior_same_day_rows(monkeypatch):
    deletes=[]
    monkeypatch.setattr(suppliers,'last_supplier_map',lambda:{})
    monkeypatch.setattr(suppliers,'_ms',lambda *a,**k:{'rows':[dict(name='Empty',meta={'href':'store'})]})
    monkeypatch.setattr(suppliers,'fetch_stock_store',lambda *a:[])
    monkeypatch.setattr(suppliers.db,'upsert',lambda *a,**k:0)
    monkeypatch.setattr(suppliers.db,'execute',lambda sql,params:deletes.append(params) or 0)
    suppliers.main(with_suppliers=False,with_turnover=False,captured='2026-11-09')
    assert deletes==[('2026-11-09','Empty',[])]
