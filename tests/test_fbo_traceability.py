# поток: fin
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from core import db
from collectors import fbo_traceability as t

NOW=datetime.now(timezone.utc)
ORIGINAL_SYNC=t.sync_origins
ORIGINAL_OZON_CANDIDATES=t.ozon_candidates


def document():
    ref=lambda entity,id:dict(meta=dict(href=f'https://api.moysklad.ru/api/remap/1.2/entity/{entity}/{id}'))
    return dict(id='doc',name='123',applicable=True,moment=(NOW-timedelta(days=3)).isoformat(),
        organization=ref('organization',t.ORGS['wb_acc1']),positions=dict(meta=dict(size=1),rows=[
            dict(quantity=1,assortment={**ref('product','product'),'externalCode':'0738'})]))


def report():
    return dict(rows=[dict(positions=[dict(meta=dict(href='https://api.moysklad.ru/api/remap/1.2/entity/product/product'),
        cost=10000,quantity=999999)])])


def source(**kw):
    return dict(id=1,account='wb_acc1',posting_number='fbo1',created_at=NOW-timedelta(days=1),
        products=[dict(sku='100',offer_id='0738WB',quantity=1)],unit_id='12345',sku='100',
        target_rrd_ids=[2],source_rrd_id=1,assembly_id='123',source_srid='fbs1',
        source_created_at=(NOW-timedelta(days=4)).isoformat(),demand_id='doc',qty=1,cogs=Decimal(100),npos=1,**kw)


def test_cost_uses_operation_fifo_and_document_quantity_not_current_stock():
    proof=t.validate_proof(document(),report())
    assert proof['cost']=='100' and proof['quantity']==1


@pytest.mark.parametrize('case',['draft','multi','different_product','zero_cost','nan'])
def test_source_proof_fails_closed(case):
    doc=document();cost=report()
    if case=='draft':doc['applicable']=False
    if case=='multi':doc['positions']['rows'][0]['quantity']=2
    if case=='different_product':cost['rows'][0]['positions'][0]['meta']['href']='other'
    if case=='zero_cost':cost['rows'][0]['positions'][0]['cost']=0
    if case=='nan':cost['rows'][0]['positions'][0]['cost']='NaN'
    with pytest.raises(ValueError):t.validate_proof(doc,cost)


def test_only_unique_physical_unit_origin_with_correct_time_is_eligible():
    good=source();assert 1 in t.choose_origins([good])[0]
    for other in (dict(good,unit_id='other'),dict(good,assembly_id='456')):
        assert not t.choose_origins([good,other])[0]
    assert not t.choose_origins([dict(good,target_unit_count=2)])[0]
    assert not t.choose_origins([dict(good,source_created_at=NOW.isoformat())])[0]
    assert not t.choose_origins([dict(good,products=[dict(sku='wrong',quantity=1)])])[0]
    assert 1 in t.choose_origins([dict(good,demand_id=None,cogs=None,qty=None,npos=None)])[0]
    assert 1 in t.choose_origins([dict(good,assembly_id='12345678-0001-1',account='oz_acc1')])[0]


@pytest.fixture
def isolated(monkeypatch):
    schema='fbo_trace_test_'+uuid4().hex;original=db.get_conn;root=Path(__file__).parents[1]
    with original() as conn,conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"');cur.execute(f'SET LOCAL search_path TO "{schema}"')
        for file in ('000_fbo_traceability.sql','087_wb_fbo_intraday_captures.sql','095_fbo_orders_restore.sql',
                     '099_fbo_inventory_multichannel.sql','090_wb_return_journey.sql'):
            cur.execute((root/'migrations'/file).read_text())
        cur.execute('CREATE TABLE ms_return_cogs(demand_name text,org text,store text,moment timestamptz)')
        cur.execute('CREATE TABLE mp_stock_capture(platform text,account text,captured_at date,recorded_at timestamptz)')
    @contextmanager
    def connect():
        with original() as conn:
            with conn.cursor() as cur:cur.execute(f'SET LOCAL search_path TO "{schema}"')
            yield conn
    monkeypatch.setattr(db,'get_conn',connect)
    monkeypatch.setattr(t,'LOCK',7348300000+int(uuid4().hex[:6],16))
    monkeypatch.setattr(t,'sync_origins',lambda cur:0)
    monkeypatch.setattr(t,'ozon_candidates',lambda:[])
    try:yield
    finally:
        with original() as conn,conn.cursor() as cur:cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_confirmed_cost_history_is_idempotent_and_conflict_does_not_erase_history(isolated,monkeypatch):
    from psycopg2.extras import Json
    db.execute('''INSERT INTO mp_fbo_orders(id,platform,account,posting_number,created_at,observed_at,
      mp_status,source_status,products,source_api,input_hash) VALUES
      (1,'wb','wb_acc1','fbo1',%s,now(),'created','created',%s,'test','hash')''',
      (NOW-timedelta(days=1),Json(source()['products'])))
    proof=t.validate_proof(document(),report())
    db.execute('INSERT INTO mp_fbo_ms_cost_proof VALUES(%s,now(),%s,%s,%s)',
               ('doc',Json(document()),Json(report()),Json(proof)))
    monkeypatch.setattr(t,'wb_candidates',lambda:[source()])
    monkeypatch.setattr(t,'fetch_proof',lambda *a:pytest.fail('Cached proof must be reused'))
    assert t.run(0)['confirmed_cost']==1
    assert t.run(0)['history_events_added']==0
    assert db.query('SELECT cost FROM mp_fbo_trace_state')[0]['cost']==100
    from web.customer_guard_api import fbo_history
    history=fbo_history(1)
    assert history['fbo_trace']['state']=='confirmed'
    assert history['fbo_trace']['cost']=='100'
    assert len(history['trace_history'])==2
    db.execute("INSERT INTO mp_fbo_source_lookup(account,fbs_posting_number,demand_id) VALUES('wb_acc1','123','doc')")
    monkeypatch.setattr(t,'wb_candidates',lambda:[dict(source(),demand_id=None,cogs=None,qty=None,npos=None)])
    assert t.run(0)['confirmed_cost']==1
    assert t.run(0)['history_events_added']==0
    db.execute('INSERT INTO ms_return_cogs VALUES(%s,%s,%s,%s)',
               ('123',t.ORGS['wb_acc1'],'Звездный',NOW-timedelta(days=2)))
    assert t.run(0)['confirmed_cost']==0
    assert db.query('SELECT cost FROM mp_fbo_trace_state')[0]['cost'] is None
    assert db.query("SELECT count(*) n FROM mp_fbo_trace_event WHERE source_kind='cost_assessment'")[0]['n']==2
    assert db.query("SELECT count(*) n FROM mp_fbo_trace_event WHERE source_kind='fbs_origin'")[0]['n']==1


def test_database_rejects_confirmed_cost_without_proof(isolated):
    with pytest.raises(Exception):
        db.execute("INSERT INTO mp_fbo_trace_state VALUES(1,'confirmed',NULL,'{}','hash',now())")


def test_new_proof_is_archived_and_first_replay_is_idempotent(isolated,monkeypatch):
    from psycopg2.extras import Json
    db.execute('''INSERT INTO mp_fbo_orders(id,platform,account,posting_number,created_at,observed_at,
      mp_status,source_status,products,source_api,input_hash) VALUES
      (1,'wb','wb_acc1','fbo1',%s,now(),'created','created',%s,'test','hash')''',
      (NOW-timedelta(days=1),Json(source()['products'])))
    monkeypatch.setattr(t,'wb_candidates',lambda:[source()])
    monkeypatch.setattr(t,'fetch_proof',lambda *a:(document(),report(),t.validate_proof(document(),report())))
    assert t.run(1)['confirmed_cost']==1
    assert t.run(0)['history_events_added']==0
    assert db.query('SELECT count(*) n FROM mp_fbo_ms_cost_proof_event')[0]['n']==1


def test_origin_tracks_marketplace_receipt_and_later_departure_without_losing_history(isolated):
    from psycopg2.extras import Json,RealDictCursor
    db.execute('CREATE TABLE customer_guard_orders(id bigint,latest_check_id bigint,mp_payload jsonb)')
    db.execute('CREATE TABLE customer_guard_checks(id bigint,result jsonb)')
    db.execute('CREATE TABLE raw_mp_returns(platform text,account text,return_id text,payload jsonb)')
    raw=dict(schema='Fbs',posting_number='123-0001-1',product=dict(sku=100,quantity=1,offer_id='0738'),
             visual=dict(status=dict(sys_name='ReturnedToOzon'),change_moment=NOW.isoformat()),
             exemplars=[dict(exemplar_id=12345)])
    db.execute("INSERT INTO raw_mp_returns VALUES('ozon','oz_acc1','ret',%s)",(Json(raw),))
    with db.get_conn() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert ORIGINAL_SYNC(cur)==1
    first=db.query('SELECT stage,received_at FROM mp_fbo_return_origin')[0]
    assert first['stage']=='received_marketplace'
    raw['visual']['status']['sys_name']='ReceivedBySeller'
    db.execute('UPDATE raw_mp_returns SET payload=%s',(Json(raw),))
    for _ in range(2):
        with db.get_conn() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:ORIGINAL_SYNC(cur)
    assert db.query('SELECT stage,received_at FROM mp_fbo_return_origin')[0]==dict(stage='ozon:ReceivedBySeller',received_at=first['received_at'])
    assert db.query('SELECT count(*) n FROM mp_fbo_return_origin_event')[0]['n']==2


def test_ozon_uses_shared_exemplar_not_return_record_id_and_keeps_accounts_separate(isolated,monkeypatch):
    from psycopg2.extras import Json
    db.execute('CREATE TABLE raw_mp_returns(platform text,account text,return_id text,payload jsonb)')
    db.execute('CREATE TABLE ms_demand_cogs(demand_id text,demand_name text,org text,qty numeric,cogs numeric,npos integer)')
    db.execute('''INSERT INTO mp_fbo_orders(id,platform,account,posting_number,created_at,observed_at,
      mp_status,source_status,products,source_api,input_hash) VALUES
      (1,'ozon','oz_acc1','fbo1',%s,now(),'returned','returned',%s,'test','hash')''',
      (NOW-timedelta(days=1),Json(source()['products'])))
    original=dict(schema='Fbs',posting_number='123-0001-1',product=dict(sku=100),
        exemplars=[dict(id=999,exemplar_id=12345)],visual=dict(status=dict(sys_name='ReturnedToOzon')),
        logistic=dict(final_moment=(NOW-timedelta(days=2)).isoformat()))
    target=dict(schema='Fbo',posting_number='fbo1',product=dict(sku=100),exemplars=[dict(id=888,exemplar_id=12345)])
    db.execute('INSERT INTO raw_mp_returns VALUES(%s,%s,%s,%s)',('ozon','oz_acc1','source',Json(original)))
    db.execute('INSERT INTO raw_mp_returns VALUES(%s,%s,%s,%s)',('ozon','oz_acc1','target',Json(target)))
    db.execute('INSERT INTO raw_mp_returns VALUES(%s,%s,%s,%s)',('ozon','oz_acc2','other',Json(original)))
    doc=document();doc['name']='123-0001-1';proof=t.validate_proof(doc,report())
    db.execute('INSERT INTO ms_demand_cogs VALUES(%s,%s,%s,1,100,1)',('doc',doc['name'],t.ORGS['oz_acc1']))
    db.execute('INSERT INTO mp_fbo_ms_cost_proof VALUES(%s,now(),%s,%s,%s)',('doc',Json(doc),Json(report()),Json(proof)))
    monkeypatch.setattr(t,'wb_candidates',lambda:[])
    # The fixture disables only the Ozon selector for unrelated WB tests.
    monkeypatch.setattr(t,'ozon_candidates',ORIGINAL_OZON_CANDIDATES)
    assert len(t.ozon_candidates())==1
    assert t.run(0)['confirmed_cost']==1
    state=db.query('SELECT evidence FROM mp_fbo_trace_state')[0]['evidence']
    assert state['unit_kind']=='ozon_exemplar' and state['unit_id']=='12345'
    assert state['source_return_ids']==['source']
