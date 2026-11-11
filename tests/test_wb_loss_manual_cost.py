# поток: fin
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg2.extras import Json
import pytest
from core import db
from web.warehouse_loss_api import router


def payload():
    rows=[dict(account='wb_acc1',warehouse='A',nm_id=123,qty=3,costed_qty=1,known_cost_rub='100'),
          dict(account='wb_acc1',warehouse='A',nm_id=456,qty=2,costed_qty=2,known_cost_rub='200'),
          dict(account='wb_acc1',warehouse='B',nm_id=123,qty=1,costed_qty=1,known_cost_rub='50')]
    second=[dict(account='wb_acc2',warehouse='A',nm_id=123,qty=3,costed_qty=0,known_cost_rub='0')]
    return dict(mode='recovered_snapshot',accounts={
        'wb_acc1':dict(items=rows,summary=dict(qty=6,costed_qty=4,known_cost_rub='350')),
        'wb_acc2':dict(items=second,summary=dict(qty=3,costed_qty=0,known_cost_rub='0'))})


@pytest.fixture
def client(monkeypatch):
    schema='loss_manual_test_'+uuid4().hex;original=db.get_conn
    root=Path(__file__).parents[1]
    with original() as c,c.cursor() as q:
        q.execute(f'CREATE SCHEMA "{schema}"');q.execute(f'SET LOCAL search_path TO "{schema}"')
        q.execute('CREATE TABLE wb_damage_stock_warehouse(warehouse text PRIMARY KEY)')
        for n in ['512_wb_loss_official.sql','097_wb_loss_recovery_snapshot.sql','098_wb_loss_manual_cost.sql']:
            q.execute((root/'migrations'/n).read_text())
    @contextmanager
    def isolated():
        with original() as c:
            with c.cursor() as q:q.execute(f'SET LOCAL search_path TO "{schema}"')
            yield c
    monkeypatch.setattr(db,'get_conn',isolated)
    db.execute('INSERT INTO wb_loss_recovery_snapshot(id,payload) VALUES(%s,%s)',('test',Json(payload())))
    app=FastAPI();app.include_router(router)
    with TestClient(app) as c:
        yield c
    with original() as c,c.cursor() as q:q.execute(f'DROP SCHEMA "{schema}" CASCADE')


def body(**kw):
    return dict(snapshot_id='test',account='wb_acc1',warehouse='A',nm_id=123,version=0,
                expected_missing_qty=2,unit_cost='12.35',note='Стоимость целого набора',**kw)


def read(c):return c.get('/api/warehouse/loss').json()


def test_save_reload_update_clear_and_audit_keep_ms_cost_and_source_unchanged(client):
    b=body();r=client.post('/api/warehouse/loss/manual-cost',json=b,headers={'X-Pult-User':'operator'})
    assert r.status_code==200
    d=read(client);i=d['accounts']['wb_acc1']['items'][0];s=d['accounts']['wb_acc1']['summary']
    assert i['known_cost_rub']=='100' and i['costed_qty']==1
    assert i['manual_costed_qty']==2 and i['manual_cost_rub']=='24.70'
    assert i['total_cost_rub']=='124.70' and i['remaining_qty']==0
    assert s['total_cost_rub']=='374.70' and s['total_costed_qty']==6
    assert d['accounts']['wb_acc2']['items'][0]['manual'] is None
    assert d['accounts']['wb_acc1']['items'][2]['manual'] is None
    assert db.query("SELECT payload FROM wb_loss_recovery_snapshot WHERE id='test'")[0]['payload']==payload()
    r=client.post('/api/warehouse/loss/manual-cost',json=dict(b,version=1,unit_cost='20'))
    assert r.status_code==200 and read(client)['accounts']['wb_acc1']['items'][0]['manual_cost_rub']=='40.00'
    r=client.request('DELETE','/api/warehouse/loss/manual-cost',json=dict(b,version=2))
    assert r.status_code==200
    d=read(client);assert d['accounts']['wb_acc1']['summary']['total_cost_rub']=='350'
    assert d['accounts']['wb_acc1']['items'][0]['manual']['enabled'] is False
    history=db.query('SELECT action,old_value,new_value,actor FROM wb_loss_manual_cost_history ORDER BY id')
    assert [r['action'] for r in history]==['save','save','clear']
    assert history[0]['old_value'] is None and history[0]['actor']=='operator'
    assert history[-1]['old_value']['enabled'] and not history[-1]['new_value']['enabled']


@pytest.mark.parametrize('price',['0','-1','NaN','Infinity','1.234','1000000000000',''])
def test_invalid_money_does_not_change_data(client,price):
    r=client.post('/api/warehouse/loss/manual-cost',json=dict(body(),unit_cost=price))
    assert r.status_code==422
    assert db.query('SELECT count(*) n FROM wb_loss_manual_cost')[0]['n']==0


def test_stale_edit_or_missing_quantity_conflict_is_not_overwritten(client):
    assert client.post('/api/warehouse/loss/manual-cost',json=dict(body(),expected_missing_qty=1)).status_code==409
    assert client.post('/api/warehouse/loss/manual-cost',json=body()).status_code==200
    assert client.post('/api/warehouse/loss/manual-cost',json=dict(body(),unit_cost='99')).status_code==409
    assert read(client)['accounts']['wb_acc1']['items'][0]['manual']['unit_cost']=='12.35'


def test_cannot_write_to_full_ms_cost_unknown_item_or_other_origin(client):
    assert client.post('/api/warehouse/loss/manual-cost',json=dict(body(),nm_id=456,expected_missing_qty=0)).status_code==409
    assert client.post('/api/warehouse/loss/manual-cost',json=dict(body(),warehouse='unknown')).status_code==404
    assert client.post('/api/warehouse/loss/manual-cost',json=body(),headers={'Origin':'https://another.test'}).status_code==403
    assert db.query('SELECT count(*) n FROM wb_loss_manual_cost_history')[0]['n']==0


@pytest.mark.parametrize('qty,costed,manual_expected',[(3,2,1),(10,1,2),(3,3,0)])
def test_recalculation_uses_ms_first_and_does_not_expand_manual_quantity(client,qty,costed,manual_expected):
    assert client.post('/api/warehouse/loss/manual-cost',json=body()).status_code==200
    p=payload();i=p['accounts']['wb_acc1']['items'][0];i['qty']=qty;i['costed_qty']=costed
    db.execute('UPDATE wb_loss_recovery_snapshot SET payload=%s WHERE id=%s',(Json(p),'test'))
    i=read(client)['accounts']['wb_acc1']['items'][0]
    assert i['manual_costed_qty']==manual_expected
    assert i['manual']['qty']==2
