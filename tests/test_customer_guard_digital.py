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
from customer_guard import domain, store, runtime, notifications as n, wb, yandex
from customer_guard.sources import CustomerMS
from shipment_guard import config
from shipment_guard.ms import SourceError
from tests.test_customer_guard import pg, posting, ingest, NOW, rule
from tests.test_customer_guard_wb import order
from tests.test_customer_guard_runtime import activate, fake_transport


def ya_order():
    return dict(orderId=123456789,campaignId=22,programType='FBS',fake=False,
        creationDate='2026-10-21T13:40:34.66+03:00',status='PROCESSING',substatus='STARTED',
        delivery={'address':'private'},buyer={'phone':'private'},
        items=[dict(offerId='0001',count=2,prices={'payment':{'value':600,'currencyId':'RUR'},
            'subsidy':{'value':100,'currencyId':'RUR'},'cashback':{'value':40,'currencyId':'RUR'}})])


@pytest.mark.parametrize('status,expected', [('PROCESSING','awaiting_packaging'),('DELIVERY','delivering'),
    ('PICKUP','delivering'),('DELIVERED','delivered'),('CANCELLED','cancelled'),('UNPAID','unknown'),('NEW_STATE','unknown')])
def test_yandex_normalization(status,expected):
    raw=ya_order();raw['status']=status;p=yandex.normalize(raw)
    assert p['status']==expected
    assert domain.sale(p)==(Decimal(740),Decimal(2))
    assert 'delivery' not in p['market_order'] and 'buyer' not in p['market_order']
    assert p['created_at']=='2026-10-21T13:40:34.660000+03:00'


@pytest.mark.parametrize('key,value',[('programType','FBY'),('fake',True),('orderId',True)])
def test_yandex_scope(key,value):
    raw=ya_order();raw[key]=value
    with pytest.raises(SourceError):yandex.normalize(raw)


def test_yandex_api_pagination_exact_identity_and_budget(monkeypatch):
    monkeypatch.setenv('YANDEX_BUSINESS_ID_ACC1','123');monkeypatch.setenv('YANDEX_API_KEY_ACC1','dummy')
    req=Mock(return_value=Mock(status_code=200,json=lambda:{'orders':[ya_order()],'paging':{}}))
    client=yandex.YandexClient(request=req,sleep=lambda _:None,budget=1)
    p=client.fetch(NOW-timedelta(days=1),NOW)
    assert req.call_args.args[0].endswith('/v1/businesses/123/orders')
    body=req.call_args.kwargs['json']
    assert body['programTypes']==['FBS'] and body['fake'] is False and 'dates' in body
    with pytest.raises(SourceError,match='budget'):client.get_post(p[0])
    client=yandex.YandexClient(sleep=lambda _:None)
    client.call=Mock(return_value={'orders':[ya_order()],'paging':{'nextPageToken':'repeat'}})
    with pytest.raises(SourceError,match='cursor'):client.fetch(NOW-timedelta(days=1),NOW)
    client.call=Mock(return_value={'orders':[]})
    with pytest.raises(SourceError,match='not_found'):client.get_post(p[0])
    changed=ya_order();changed['campaignId']=333;client.call=Mock(return_value={'orders':[changed]})
    with pytest.raises(SourceError,match='campaign'):client.get_post(p[0])


@pytest.mark.parametrize('account',list(domain.ACCOUNTS))
def test_every_cabinet_has_separate_ms_org_agent_and_floor(account):
    platform=domain.ACCOUNTS[account];org=domain.organization(account)
    client=object.__new__(CustomerMS);client.account=account
    num='123-1-1' if platform=='ozon' else '123456789'
    agent=next(k for k,v in config.AGENTS.items() if v==platform)
    doc=dict(name=num,organization={'meta':{'href':config.MS+'/entity/organization/'+org}},
        agent={'meta':{'href':config.MS+'/entity/counterparty/'+agent}})
    client.collection=Mock(return_value=[doc]);assert client.find_order(num)==doc
    assert org in client.collection.call_args.args[1]['filter']
    p=posting();p.update(account=account,platform=platform)
    r=rule();r.update(account=account,platform=platform,organization_id=org)
    v=domain.economics(p,Decimal(150),'purchase_estimate',r,NOW)
    assert v['floor']=='388.89'
    r['organization_id']=domain.organization('oz_acc2' if account.endswith('acc1') else 'oz_acc1')
    assert domain.economics(p,Decimal(150),'purchase_estimate',r,NOW)['floor'] is None
    doc['organization']['meta']['href']=config.MS+'/entity/organization/'+r['organization_id']
    with pytest.raises(SourceError,match='mismatch'):client.find_order(num)


def test_wb_tokens_are_account_scoped(monkeypatch):
    monkeypatch.setenv('SHIPMENT_GUARD_WB_TOKEN_ENV_ACC1','WB_TOKEN_RETURNS_ACC1')
    monkeypatch.setenv('WB_TOKEN_RETURNS_ACC1','one');monkeypatch.setenv('WB_TOKEN_ACC2','two')
    req=Mock(return_value=Mock(status_code=200,json=lambda:{'orders':[]}))
    wb.WBClient(account='wb_acc1',request=req).call('/api/v3/orders/new')
    assert req.call_args.kwargs['headers']['Authorization']=='one'
    monkeypatch.setenv('SHIPMENT_GUARD_WB_TOKEN_ENV_ACC1','WB_TOKEN_RETURNS_ACC2')
    with pytest.raises(SourceError,match='reference'):wb.WBClient(account='wb_acc1',request=req).call('/api/v3/orders/new')


def test_all_cabinets_api_queue_and_nonpositive_filter(pg,monkeypatch):
    posts=[]
    for a,p in domain.ACCOUNTS.items():
        value=posting('123456789');value.update(account=a,platform=p);posts.append(value)
    ingest(posts);at=NOW+timedelta(minutes=10)
    for acc in domain.ACCOUNTS:
        job=store.claim(at,'123456789',acc,accounts=[acc]);assert job['order']['account']==acc
        store.finish(job,dict(signal='below_floor',floor='500',estimated_net='-1',rule_version=acc),at)
    assert store.claim(at,accounts=['oz_acc2']) is None
    from web import customer_guard_api as api
    monkeypatch.setattr(api,'get_basis',lambda acc:{'version':acc})
    app=FastAPI();app.include_router(api.router);c=TestClient(app)
    assert c.get('/api/customer-orders',params={'signal':'non_positive_profit'}).json()['total']==5
    for acc in domain.ACCOUNTS:
        rows=c.get('/api/customer-orders',params={'account':acc}).json()['rows']
        assert len(rows)==1 and rows[0]['account']==acc


@pytest.mark.parametrize('account,platform,label',[('oz_acc1','ozon','Ozon'),('wb_acc1','wb','WB'),('ya_acc1','yandex','Маркет')])
def test_digital_missing_notifications_gate(pg,monkeypatch,account,platform,label):
    p=posting();p.update(account=account,platform=platform);ingest([p]);activate();at=NOW+timedelta(minutes=10)
    job=store.claim(at,account=account);assert store.refresh(job,p,at)
    store.finish(job,dict(signal='missing_ms'),at)
    fake_transport(monkeypatch,at);monkeypatch.setattr(n,'utcnow',lambda:at)
    monkeypatch.setattr(n,'get_basis',Mock(side_effect=n.BasisUnavailable('missing')))
    send=Mock(return_value=Mock(status_code=200,json=lambda:{'ok':True,'result':{'message_id':1}}))
    monkeypatch.setattr(n.requests,'post',send)
    assert n.send_current(job['order_id'])=='stale_or_disabled' and not send.called
    db.execute('''UPDATE customer_guard_runtime SET settings=settings || '{"digital_enabled":true}'::jsonb''')
    assert n.send_current(job['order_id'])=='sent'
    assert label+' · Цифровой' in send.call_args.kwargs['json']['text']


def test_runtime_one_shared_ms_budget_for_five_cabinets(pg,monkeypatch):
    activate();db.execute('''UPDATE customer_guard_runtime SET settings=settings || '{"digital_enabled":true,"wb_enabled":true}'::jsonb''')
    monkeypatch.setattr(runtime,'RUNTIME_LOCK',uuid4().int%(2**63-1))
    for name in ('sync','sync_wb','sync_yandex','refresh','refresh_wb','refresh_yandex','reprice','work'):
        monkeypatch.setattr(runtime.service,name,Mock(return_value={}))
    runtime.tick()
    call=runtime.service.work
    assert call.call_count==1
    assert call.call_args.kwargs['limit']==3 and call.call_args.kwargs['budget']==100
    assert set(call.call_args.kwargs['accounts'])==set(domain.ACCOUNTS)
    assert runtime.service.sync.call_count==2 and runtime.service.sync_wb.call_count==2
    assert runtime.service.sync_yandex.call_count==1
