# поток: fin
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock

import pytest

from core import db
from collectors import wb_fbo_auto_link as wb, wb_fbo_intraday as stock, ozon_fbo_intraday as ozon

NOW=datetime.now(timezone.utc)


def observations():
    return [dict(id=i+1,seq=i+1,nm_id=101,completed_at=NOW+timedelta(hours=i),
                 source_api='same',quantity=q,transit=t)
            for i,(q,t) in enumerate([(1,0),(0,0),(0,1)])]


def test_delayed_wb_counters_produce_explicit_candidate():
    moves=wb.delayed_moves(observations())
    assert len(moves)==1 and moves[0]['delayed']
    order=dict(id=1,created_at=NOW-timedelta(minutes=6),warehouse='Склад WB',
               products=[dict(sku='101',quantity=1)])
    assert wb.choose_matches(moves,[order])==[(moves[0],order)]
    other=dict(order,id=2,created_at=NOW-timedelta(minutes=10))
    assert wb.choose_matches(moves,[order,other])==[]


@pytest.mark.parametrize('change', ['source','missing','restock','transit_ambiguous','too_late','long_gap'])
def test_delayed_wb_does_not_bridge_ambiguous_or_partial_observations(change):
    rows=observations()
    if change=='source':rows[2]['source_api']='other'
    if change=='missing':rows[2]['seq']=4
    if change=='restock':rows[2]['quantity']=1
    if change=='transit_ambiguous':rows[2]['transit']=2
    if change=='too_late':rows[2]['completed_at']=NOW+timedelta(hours=6)
    if change=='long_gap':rows[0]['completed_at']=NOW-timedelta(hours=20)
    assert wb.delayed_moves(rows)==[]


def oz_move(**kw):
    return dict(sku='101',warehouse='A',stock_drop=1,reserve_rise=1,promised_before=0,
                promised_after=0,previous_warehouses=1,current_warehouses=1,
                previous_at=NOW,current_at=NOW+timedelta(hours=1),**kw)


def oz_order(**kw):
    return dict(id=1,created_at=NOW+timedelta(minutes=20),warehouse=None,
                products=[dict(sku='101',quantity=1)],**kw)


def test_ozon_requires_reserve_and_unique_warehouse_or_explicit_order_warehouse():
    move=oz_move();order=oz_order()
    assert len(ozon.choose_matches([move],[order]))==1
    assert ozon.choose_matches([dict(move,reserve_rise=2)],[order])==[]
    assert ozon.choose_matches([dict(move,promised_after=1)],[order])==[]
    assert ozon.choose_matches([dict(move,previous_warehouses=2)],[order])==[]
    assert len(ozon.choose_matches([dict(move,previous_warehouses=2)], [dict(order,warehouse='A')]))==1
    assert ozon.choose_matches([move],[dict(order,warehouse='B')])==[]
    assert ozon.choose_matches([move],[order,dict(order,id=2)])==[]
    assert ozon.choose_matches([move],[dict(order,mp_status='cancelled')])==[]


def test_ozon_complete_stock_normalization_and_duplicate_rejection():
    raw=dict(sku=101,warehouse_name='A',free_to_sell_amount=1,reserved_amount=0,promised_amount=0)
    assert ozon.normalize([raw])[0][:5]==('101','A',1,0,0)
    assert ozon.normalize([])==[]
    for rows in ([raw,raw],[dict(raw,reserved_amount=None)],[dict(raw,free_to_sell_amount=-1)]):
        with pytest.raises(ValueError):ozon.normalize(rows)


@pytest.fixture
def isolated(monkeypatch):
    schema='fbo_multi_test_'+uuid4().hex;original=db.get_conn
    root=Path(__file__).parents[1]
    with original() as conn,conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET LOCAL search_path TO "{schema}"')
        for file in ('087_wb_fbo_intraday_captures.sql','095_fbo_orders_restore.sql','099_fbo_inventory_multichannel.sql'):
            cur.execute((root/'migrations'/file).read_text())
        cur.execute('''CREATE TABLE mp_stock_history(platform text,account text,sku text,warehouse text,
          captured_at date,usable_available numeric,raw_available numeric,excluded_loss numeric,
          usable_total numeric,reserved numeric,promised numeric,in_way_to_client numeric,in_way_from_client numeric)''')
    @contextmanager
    def connect():
        with original() as conn:
            with conn.cursor() as cur:cur.execute(f'SET LOCAL search_path TO "{schema}"')
            yield conn
    monkeypatch.setattr(db,'get_conn',connect)
    try:yield
    finally:
        with original() as conn,conn.cursor() as cur:cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


def insert_order(platform,account,sku='101'):
    return db.query('''INSERT INTO mp_fbo_orders
      (platform,account,posting_number,created_at,observed_at,mp_status,source_status,warehouse,
       products,source_api,input_hash) VALUES(%s,%s,%s,%s,%s,'created','created',%s,
       %s::jsonb,'test','test') RETURNING id''',
      (platform,account,str(uuid4()),NOW-timedelta(minutes=20),NOW,
       'Склад WB' if platform=='wb' else None,'[{"sku":"'+sku+'","quantity":1}]'))[0]['id']


def test_wb_diskver_capture_and_link_are_isolated_idempotent(isolated,monkeypatch):
    insert_order('wb','wb_acc2')
    for index,(q,t) in enumerate(((1,0),(0,1))):
        at=NOW-timedelta(minutes=40-30*index)
        cap=db.query('''INSERT INTO wb_fbo_intraday_capture
          (account,started_at,completed_at,source_api,source_rows,quantity,in_way_to_client,in_way_from_client)
          VALUES('wb_acc2',%s,%s,'same',1,%s,%s,0) RETURNING id''',(at,at,q,t))[0]['id']
        db.execute('''INSERT INTO wb_fbo_intraday_item VALUES(%s,101,1,1,'Склад WB',NULL,%s,%s,0,'{}')''',(cap,q,t))
    assert wb.run('wb_acc1')['inserted']==0
    assert wb.run('wb_acc2')['inserted']==1
    assert wb.run('wb_acc2')['inserted']==0
    link=db.query('select account,evidence from mp_fbo_intraday_link_candidates')[0]
    assert link['account']=='wb_acc2' and link['evidence']['cogs_assigned'] is False


def test_ozon_both_accounts_link_only_their_own_orders(isolated):
    for account in ozon.ACCOUNTS:
        insert_order('ozon',account)
        for index,(q,r) in enumerate(((2,0),(1,1))):
            at=NOW-timedelta(minutes=40-30*index)
            cap=db.query('''INSERT INTO ozon_fbo_intraday_capture
              (account,started_at,completed_at,source_rows,source_api) VALUES(%s,%s,%s,1,'same') RETURNING id''',
              (account,at,at))[0]['id']
            db.execute("INSERT INTO ozon_fbo_intraday_item VALUES(%s,'101','A',%s,%s,0,'{}')",(cap,q,r))
        assert ozon.run(account)['inserted']==1
        assert ozon.run(account)['inserted']==0
    assert db.query('select count(*) n from ozon_fbo_stock_link_candidates')[0]['n']==2
    from web.mp_inventory_api import stock_history
    history=stock_history('ozon','oz_acc1','101','A')
    assert len(history['links'])==1 and len(history['intraday'])==2
    assert history['intraday'][0]['reserved']==1
    assert history['links'][0]['evidence']['cogs_assigned'] is False
    assert stock_history('ozon','oz_acc1','101','B')['links']==[]


def test_partial_ozon_fetch_does_not_publish_snapshot(isolated,monkeypatch):
    monkeypatch.setattr(ozon,'fetch',Mock(side_effect=RuntimeError('partial pagination')))
    with pytest.raises(RuntimeError):ozon.collect('oz_acc2')
    assert db.query('select count(*) n from ozon_fbo_intraday_capture')[0]['n']==0


def test_inventory_coordinator_continues_after_one_account_failure(monkeypatch):
    from collectors import mp_fbo_inventory as coordinator
    called=[]
    def collect(account):
        called.append(account)
        if account=='wb_acc1':raise RuntimeError('secret URL')
        return {'ok':True}
    monkeypatch.setattr(coordinator.wb,'collect',collect)
    monkeypatch.setattr(coordinator.ozon,'collect',collect)
    assert coordinator.main()==1
    assert called==['wb_acc1','wb_acc2','oz_acc1','oz_acc2']
