# поток: inv
"""Real PostgreSQL in disposable isolated schemas; production rows are never mutated."""
import ast
import contextlib
import datetime as dt
import json
from pathlib import Path
import threading
import uuid
import psycopg2
from psycopg2 import sql
import pytest
from core import db,opex_close as c
from collectors import bank_txn_store

CK='7807355364';DK='7811803918';OCT=dt.date(2026,10,1)


def statement_builder():
    names={'opex_statement','_opex_month','_opex_spread','_opex_start_month','opex_assign','_opex_is_fact'}
    nodes=[n for n in ast.parse(Path('web/app.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name in names]
    for n in nodes:
        n.decorator_list=[]
        if n.name=='opex_assign':n.args.args[0].annotation=None
    scope={'db':db,'_oc':c,'OPEX_FACT_SINCE':'2026-08-01','ORG_NAMES':{CK:'CK',DK:'DK'},'ORG_ORDER':[CK,DK],'txn_store':bank_txn_store}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'web/app.py','exec'),scope)
    return scope


@pytest.fixture
def env(monkeypatch,tmp_path):
    monkeypatch.setenv("OPEX_CLOSE_ARCHIVE_DIR",str(tmp_path/"archive"))
    schema='test_opex_close_'+uuid.uuid4().hex
    conn=psycopg2.connect(db.DATABASE_URL)
    with conn,conn.cursor() as q:
        q.execute(sql.SQL('create schema {}').format(sql.Identifier(schema)))
        defs={}
        for view in ['opex_rule_reach','opex_rule_match']:
            q.execute('select pg_get_viewdef(%s::regclass,true)',('public.'+view,));defs[view]=q.fetchone()[0].replace('public.',schema+'.')
        q.execute(sql.SQL('set local search_path to {}').format(sql.Identifier(schema)))
        for table in ['bank_txn','bank_txn_opex','opex_category','opex_rule']:
            q.execute(sql.SQL('create table {} (like public.{} including defaults including constraints including indexes)').format(sql.Identifier(table),sql.Identifier(table)))
        for view,ddl in defs.items():q.execute('create view '+view+' as '+ddl)
        q.execute(Path('migrations/229_opex_fact_exclusion_repair.sql').read_text())
        q.execute('''create view opex_fact_month as select month,org_inn,category_id,category,sum(amount) amount,count(*)::int txn_count from opex_fact_alloc group by 1,2,3,4''')
        q.execute(Path('migrations/230_opex_month_closure.sql').read_text())
        q.execute("insert into opex_category(id,name,in_opex) values(1,'ФОТ',true),(2,'Other',true),(8,'Закупка товара у поставщиков',false),(9,'Перевод между своими счетами',false)")
        for id,org,date,amount,party in [(1,CK,'2026-09-10',900,'payee'),(2,CK,'2026-10-10',100,'tax'),(3,CK,'2026-10-10',1000,'vendor'),(4,CK,'2026-10-10',2000,CK),(5,CK,'2026-10-12',50,'payee'),(6,DK,'2026-10-10',700,'payee')]:
            q.execute("insert into bank_txn(id,bank,org_inn,account,nk,direction,amount,operation_date,cp_name,cp_inn,purpose) values(%s,'alfa',%s,'a',%s,'DEBIT',%s,%s,%s,%s,'payment')",(id,org,str(id),amount,date,party,party))
        q.execute("insert into bank_txn_opex(txn_id,category_id,start_month,spread_months,source) values(1,1,'2026-09-01',3,'rule'),(2,2,'2026-10-01',1,'manual'),(3,8,'2026-10-01',1,'rule'),(4,9,'2026-10-01',1,'rule'),(6,1,'2026-10-01',1,'rule')")
        q.execute("insert into opex_rule(id,cp_inn,category_id,spread_months) values(1,'payee',1,1)")
    conn.close()
    @contextlib.contextmanager
    def isolated():
        conn=psycopg2.connect(db.DATABASE_URL)
        try:
            with conn.cursor() as q:q.execute(sql.SQL('set search_path to {}').format(sql.Identifier(schema)))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:conn.close()
    monkeypatch.setattr(db,'get_conn',isolated)
    monkeypatch.setattr(c,'current_month',lambda:dt.date(2026,11,1))
    scope=statement_builder()
    def builder(org,month):return scope['opex_statement'](org=org,month=str(month),direction='')
    try:yield {'schema':schema,'scope':scope,'builder':builder}
    finally:
        conn=psycopg2.connect(db.DATABASE_URL)
        with conn,conn.cursor() as q:q.execute(sql.SQL('drop schema {} cascade').format(sql.Identifier(schema)))
        conn.close()


def close(env):return c.close_many([(CK,OCT)],env['builder'])[0]


def test_close_is_immutable_and_idempotent_under_new_rules(env):
    info=close(env);assert info['archived'] is True
    assert info['total']==400 and info['unassigned']['n']==1
    before=c.summary(CK,OCT);body=c.statement(CK,OCT,direction='')
    db.execute('update opex_rule set category_id=2 where id=1')
    db.execute("delete from bank_txn_opex a using opex_rule_match m where m.txn_id=a.txn_id and a.source='rule' and a.category_id<>m.category_id")
    bank_txn_store.apply_rules()
    assert c.summary(CK,OCT)==before
    assert c.statement(CK,OCT,direction='')['items']==body['items']
    assert close(env)['already_closed']
    assert db.query('select count(*) n from opex_month_closure')[0]['n']==1


def test_late_raw_payment_is_preserved_but_not_added_to_closed_totals(env):
    info=close(env)
    db.execute("insert into bank_txn(id,bank,org_inn,account,nk,direction,amount,operation_date,cp_name,cp_inn,purpose) values(99,'alfa',%s,'a','late','DEBIT',123,'2026-10-20','payee','payee','payment')",(CK,))
    assert bank_txn_store.apply_rules([99])==0
    assert c.late_count(info['id'])==1
    assert 99 not in [i['id'] for i in c.statement(CK,OCT,direction='')['items']]
    assert c.summary(CK,OCT)['total']==400


def test_database_and_api_reject_edits_affecting_closed_month(env):
    close(env)
    from types import SimpleNamespace
    # September payment also allocates to the closed October.
    r=env['scope']['opex_assign'](SimpleNamespace(txn_id=1,category_id=2,spread_months=1))
    assert r['ok'] is False and r['closed'] is True
    for query in ["update bank_txn_opex set category_id=1 where txn_id=2", "delete from bank_txn_opex where txn_id=2", "update bank_txn set amount=1 where id=2", "delete from bank_txn where id=2"]:
        with pytest.raises(psycopg2.errors.CheckViolation):db.execute(query)
    assert db.execute("delete from bank_txn_opex where txn_id=1")==0
    assert c.summary(CK,OCT)['total']==400


def test_new_month_stays_open_and_retroactive_allocation_is_blocked(env):
    close(env)
    db.execute("insert into bank_txn(id,bank,org_inn,account,nk,direction,amount,operation_date,cp_name,cp_inn,purpose) values(99,'alfa',%s,'a','new','DEBIT',123,'2026-11-02','payee','payee','payment')",(CK,))
    assert bank_txn_store.apply_rules([99])==1
    assert db.query('select category_id from bank_txn_opex where txn_id=99')[0]['category_id']==1
    assert db.execute("update bank_txn_opex set start_month='2026-10-01' where txn_id=99")==0
    with pytest.raises(psycopg2.errors.CheckViolation):db.execute("update bank_txn_opex set start_month='2026-10-01',source='manual' where txn_id=99")
    assert c.summary(CK,'2026-11') is None
    assert c.summary(DK,OCT) is None


def test_reopen_keeps_snapshot_and_allows_a_new_revision(env):
    old=close(env);assert c.reopen(CK,OCT)['reopened']
    assert c.summary(CK,OCT) is None
    db.execute('update bank_txn_opex set category_id=1 where txn_id=2')
    new=close(env);assert old['id']!=new['id']
    assert db.query('select count(*) n from opex_month_closure')[0]['n']==2
    assert db.query('select snapshot_sha256 from opex_month_closure where id=%s',(old['id'],))[0]['snapshot_sha256']==old['snapshot_sha256']


def test_category_changes_do_not_rewrite_closed_names_or_amounts(env):
    close(env);before=c.summary(CK,OCT)
    db.execute("update opex_category set name='Renamed',in_opex=false where id=1")
    assert c.summary(CK,OCT)==before
    assert db.query("select sum(amount) s from opex_fact_month where org_inn=%s and month=%s",(CK,OCT))[0]['s']==400


def test_closure_wins_race_against_a_waiting_manual_update(env):
    entered=threading.Event();release=threading.Event();writer_done=threading.Event();errors=[]
    def builder(org,month):
        body=env['builder'](org,month);entered.set();assert release.wait(5);return body
    def closer():
        try:c.close_many([(CK,OCT)],builder)
        except Exception as exc:errors.append(exc)
    def writer():
        try:db.execute('update bank_txn_opex set category_id=1 where txn_id=2')
        except Exception as exc:errors.append(exc)
        finally:writer_done.set()
    a=threading.Thread(target=closer);a.start();assert entered.wait(5)
    b=threading.Thread(target=writer);b.start()
    assert not writer_done.wait(.1);release.set();a.join(5);b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert len(errors)==1 and isinstance(errors[0],psycopg2.errors.CheckViolation)
    assert c.summary(CK,OCT)['total']==400


def test_saved_versions_cannot_be_rewritten_or_deleted(env):
    close(env)
    for query in ["update opex_closed_item set amount=0", "delete from opex_closed_alloc", "update opex_closed_txn set category='changed'", "delete from opex_month_closure", "update opex_month_closure set summary='{}'::jsonb"]:
        with pytest.raises(psycopg2.errors.CheckViolation):db.execute(query)


def test_closed_empty_month_is_known_zero_and_remains_a_fact(env):
    info=c.close_many([(CK,'2026-02')],env['builder'])[0]
    assert info['total']==0 and c.summary(CK,'2026-02')['total']==0
    assert env['scope']['_opex_is_fact']('2026-02-01') is True
    assert c.statement(CK,'2026-02')['items']==[]
