# поток: fin
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import db
from customer_guard import domain, store, service, notifications as n, wb
from customer_guard.sources import CustomerMS, ORG
from shipment_guard import config
from shipment_guard.ms import SourceError
from tests.test_customer_guard import pg, posting, ingest, NOW
from tests.test_customer_guard_runtime import activate, fake_transport


def order(oid=123456789):
    return dict(id=oid,article='0001',nmId=123,createdAt=NOW.isoformat(),price=200000,
                finalPrice=120000,currencyCode=643,convertedCurrencyCode=643,
                convertedPrice=200000,address={'private':'must not store'},comment='private')


def post(oid=123456789,supplier='new',status='waiting'):
    return wb.normalize(order(oid),dict(id=oid,supplierStatus=supplier,wbStatus=status))


@pytest.mark.parametrize('supplier,state,expected',[
    ('new','waiting','awaiting_packaging'),('confirm','waiting','awaiting_deliver'),
    ('complete','waiting','delivering'),('complete','sorted','delivering'),
    ('complete','sold','delivered'),('new','declined_by_client','cancelled'),
    ('complete','canceled_by_client','cancelled'),('cancel','waiting','cancelled'),
    ('new','new_unknown_status','unknown'),('unknown','waiting','unknown'),
])
def test_wb_status_mapping(supplier,state,expected):
    assert post(supplier=supplier,status=state)['status']==expected


def test_wb_price_currency_and_privacy():
    p=post();assert domain.sale(p)==(Decimal('2000'),Decimal(1))
    assert p['account']=='wb_acc2' and p['platform']=='wb'
    assert 'address' not in p['market_order'] and 'comment' not in p['market_order']
    raw=order();raw.update(currencyCode=933,price=8750,convertedPrice=242050)
    assert domain.sale(wb.normalize(raw,dict(id=raw['id'],supplierStatus='new',wbStatus='waiting')))[0]==Decimal('2420.50')
    raw['convertedCurrencyCode']=840
    with pytest.raises(SourceError,match='non_rub'):wb.normalize(raw,dict(id=raw['id']))
    with pytest.raises(SourceError,match='identity'):wb.normalize(order(),dict(id=1))


def test_wb_order_identity_ms_price_and_rule():
    r=dict(account='wb_acc2',platform='wb',organization_id=ORG,enabled=True,
           valid_until=NOW.date()+timedelta(days=5),retention=Decimal('.554'),version='wb-rule')
    value=domain.economics(post(),Decimal('1000'),'purchase_estimate',r,NOW,price_override='2748.30')
    assert value['price']=='2748.30' and value['estimated_net']=='225.74'
    r['account']='oz_acc2'
    assert domain.economics(post(),Decimal('1000'),'purchase_estimate',r,NOW)['floor'] is None


def test_wb_ms_filter_is_cabinet_scoped():
    client=object.__new__(CustomerMS);client.account='wb_acc2'
    calls=[]
    doc=dict(id=str(uuid4()),name='123456789',organization={'meta':{'href':config.MS+'/entity/organization/'+ORG}},
             agent={'meta':{'href':config.MS+'/entity/counterparty/de89f3ba-757f-11ee-0a80-0f8300075ceb'}})
    def collection(path,params,max_pages):calls.append(params);return [doc]
    client.collection=collection
    assert client.find_order('123456789')==doc
    assert 'de89f3ba' in calls[0]['filter'] and ORG in calls[0]['filter']
    doc['agent']['meta']['href']=config.MS+'/entity/counterparty/b7e22828-0b7c-11ee-0a80-11480012ed47'
    with pytest.raises(SourceError,match='filter_mismatch'):client.find_order('123456789')


def test_two_cabinets_do_not_share_identity_jobs_or_incidents(pg,monkeypatch):
    a=posting('123456789');b=post()
    ingest([a,b]);rows=db.query('SELECT account,id FROM customer_guard_orders ORDER BY account')
    assert len(rows)==2
    at=NOW+timedelta(minutes=10)
    for account in ('oz_acc2','wb_acc2'):
        job=store.claim(at,'123456789',account)
        assert job['order']['account']==account
        assert store.finish(job,dict(signal='missing_ms'),at)
    assert len({r['dedup_key'] for r in db.query('SELECT dedup_key FROM customer_guard_incidents')})==2
    from web import customer_guard_api as api
    app=FastAPI();app.include_router(api.router);c=TestClient(app)
    assert c.get('/api/customer-orders').json()['total']==2
    assert c.get('/api/customer-orders',params={'account':'wb_acc2'}).json()['rows'][0]['platform']=='wb'
    assert c.get('/api/customer-orders',params={'account':'wb_acc1'}).status_code==422
    ingest([post(status='canceled_by_client')],at+timedelta(seconds=1))
    active=db.query("SELECT o.account FROM customer_guard_incidents i JOIN customer_guard_orders o ON o.id=i.order_id WHERE resolved_at IS NULL")
    assert [r['account'] for r in active]==['oz_acc2']


def test_wb_notification_uses_own_basis_label_and_activation(pg,monkeypatch):
    activate();ingest([post()]);at=NOW+timedelta(minutes=10);job=store.claim(at,account='wb_acc2')
    assert store.refresh(job,post(),at)
    result=dict(signal='below_floor',price='100',cost='70',floor='1000',gap='-900',estimated_net='-25',rule_version='wb-rule',cost_kind='purchase_estimate',positions=[dict(code='0001wb',external_code='0001',quantity=1)])
    doc=dict(id=str(uuid4()),state={'name':'Неукомплектован'})
    assert store.finish(job,result,at,doc)
    fake_transport(monkeypatch,at)
    bases=[]
    def basis(account):bases.append(account);return {'version':'wb-rule'}
    monkeypatch.setattr(n,'get_basis',basis)
    send=Mock(return_value=Mock(status_code=200,json=lambda:{'ok':True,'result':{'message_id':1}}));monkeypatch.setattr(n.requests,'post',send)
    assert n.send_current(job['order_id'])=='stale_or_disabled' and not send.called
    db.execute("UPDATE customer_guard_runtime SET settings=settings || '{\"wb_enabled\":true}'::jsonb")
    assert n.send_current(job['order_id'])=='sent'
    text=send.call_args.kwargs['json']['text'];assert 'WB · Дисквэр' in text and 'Цена МС' in text and '0001wb' in text
    assert all(a=='wb_acc2' for a in bases)


def test_wb_client_allows_only_reads_and_rejects_partial_status(monkeypatch):
    monkeypatch.setenv('SHIPMENT_GUARD_WB_TOKEN_ENV_ACC2','WB_TOKEN_RETURNS_ACC2')
    monkeypatch.setenv('WB_TOKEN_RETURNS_ACC2','test-token')
    call=Mock(return_value=Mock(status_code=200,json=lambda:{'orders':[]}))
    client=wb.WBClient(request=call,sleep=lambda _:None)
    with pytest.raises(SourceError,match='not_allowed'):client.call('/api/v3/supplies')
    with pytest.raises(SourceError,match='not_allowed'):client.call('/api/v3/orders',body={})
    assert not call.called
    with pytest.raises(SourceError,match='partial_status'):client.statuses([1])
    assert call.call_args.args[:2]==('POST',wb.BASE+'/api/v3/orders/status')


def test_wb_pagination_does_not_acknowledge_partial_window(monkeypatch):
    client=wb.WBClient(sleep=lambda _:None)
    client.call=Mock(return_value={'orders':[order(i) for i in range(1,21)],'next':0})
    with pytest.raises(SourceError,match='cursor_stalled'):client.fetch(NOW-timedelta(days=1),NOW)
    client.call=Mock(return_value={'orders':[order(i) for i in range(1,21)],'next':20})
    with pytest.raises(SourceError,match='page_budget'):client.fetch(NOW-timedelta(days=1),NOW,max_pages=1)
