# поток: inv
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal
from pathlib import Path
import stat
import ast
from types import SimpleNamespace

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from core import db, supplier_purchase_history as history
from collectors import supplier_purchase_month as collector
from collectors import suppliers


def doc(identity, moment, agent='a', amount=10000):
    return dict(id=identity,moment=moment+' 12:00:00',applicable=True,sum=amount,
                agent={'meta':{'href':'https://api.moysklad.ru/api/remap/1.2/entity/counterparty/'+agent}})


def test_group_merge_counts_once_and_keeps_kopecks(monkeypatch):
    monkeypatch.setattr(collector, 'ID2GROUP', {'a':'Group','b':'Group'})
    monkeypatch.setattr(collector, '_agent_names', lambda ids: {})
    docs={'supply':[doc('1','2026-10-01'),doc('2','2026-10-02','b',149)],
          'purchasereturn':[doc('r','2026-10-03','a',49)]}
    rows=collector.aggregate(dt.date(2026,10,1),docs)
    assert len(rows)==1
    assert (rows[0]['supply_sum'],rows[0]['return_sum'])==(Decimal('101.49'),Decimal('.49'))
    assert (rows[0]['supply_docs'],rows[0]['return_docs'])==(2,1)
    docs['supply'].append(docs['supply'][0])
    with pytest.raises(ValueError):collector.aggregate(dt.date(2026,10,1),docs)


def test_incomplete_pagination_never_returns_partial_month(monkeypatch):
    calls=[]
    def get(url,params):
        calls.append(params)
        return {'meta':{'size':2},'rows':[doc('1','2026-10-01')] if params['offset']==0 else []}
    monkeypatch.setattr(collector,'_get',get)
    with pytest.raises(ValueError,match='Incomplete'):collector._fetch_docs('supply',dt.date(2026,10,1),dt.date(2026,10,31))
    assert all(p['limit']<=20 and 'expand' not in p for p in calls)


def test_rollover_skips_closed_months_and_closes_only_preceding_open_month(monkeypatch):
    monkeypatch.setattr(history,'today',lambda:dt.date(2026,12,2))
    closed=[dict(month=dt.date(2026,m,1),closed_at='closed') for m in range(1,11)]
    monkeypatch.setattr(history,'periods',lambda:closed)
    monkeypatch.setattr(history,'archive',lambda month:None)
    fetched=[];saved=[]
    monkeypatch.setattr(collector,'_fetch_docs',lambda entity,start,end:fetched.append((entity,start,end)) or [])
    monkeypatch.setattr(collector,'_agent_names',lambda ids:{})
    monkeypatch.setattr(history,'save',lambda month,rows,docs:saved.append(month) or 0)
    monkeypatch.setattr(collector,'refresh_last_purchases',lambda:None)
    collector.main()
    assert saved==[dt.date(2026,11,1),dt.date(2026,12,1)]
    assert len(fetched)==4 and fetched[0][2]==dt.date(2026,11,30)


def test_supplier_names_recover_from_exact_uuid_without_changing_price_policy(monkeypatch):
    from invoice_bot import supplier_groups
    monkeypatch.setattr(supplier_groups,'ID2NAME',{'a':'Actual Supplier'})
    def query(sql):
        if 'FROM supplier_stock' in sql:return [dict(ms_id='p',supplier='Old Supplier')]
        if 'FROM ms_product' in sql:return [dict(ms_id='p',buy_price=10,supplier_ref='counterparty/a'),dict(ms_id='z',buy_price=0)]
        return []
    monkeypatch.setattr(suppliers.db,'query',query)
    assert suppliers.last_supplier_map()=={'p':('Actual Supplier',10.0),'z':(None,None)}


def test_missing_turnover_is_unknown_and_does_not_hide_supplier():
    tree=ast.parse((Path(__file__).resolve().parents[1]/'web/app.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='suppliers')
    node.decorator_list=[]
    def query(sql,params=None):
        if 'max(captured_at)' in sql:return [{'c':'2026-11-11'}]
        if 'supplier_last_purchase' in sql or "store IN ('Удаленный склад','Транзит')" in sql:return []
        return [dict(s='Supplier',n=1,moving=None,val=50)]
    scope={'db':SimpleNamespace(query=query),'_dt':dt,'_canon_supplier':lambda name:name.lower()}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'supplier-api','exec'),scope)
    result=scope['suppliers']()
    assert len(result['suppliers'])==1
    assert result['suppliers'][0]['moving'] is None and result['turnover_known'] is False


@pytest.fixture
def sandbox(monkeypatch,tmp_path):
    conn=psycopg2.connect(db.DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE supplier_purchase_month(LIKE public.supplier_purchase_month INCLUDING ALL)')
        sql=(Path(__file__).resolve().parents[1]/'migrations/231_supplier_purchase_period.sql').read_text()
        sql=sql.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE IF NOT EXISTS')
        sql=sql.replace('supplier_purchase_history_guard','pg_temp.supplier_purchase_history_guard')
        cur.execute(sql);cur.execute(sql)
    conn.commit()
    @contextmanager
    def connection():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback();raise
    def query(sql,params=None):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql,params);return [dict(r) for r in cur.fetchall()]
    monkeypatch.setattr(db,'get_conn',connection)
    monkeypatch.setattr(db,'query',query)
    monkeypatch.setattr(history,'ARCHIVE',tmp_path/'closed')
    yield conn,query
    conn.close()


def record(month,value):
    return dict(month=month,grp='Test',supply_sum=Decimal(value),supply_docs=1,
                return_sum=Decimal(0),return_docs=0,in_group=True)


def test_closed_month_is_immutable_in_db_and_archived(sandbox):
    conn,query=sandbox
    month=(history.today().replace(day=1)-dt.timedelta(days=1)).replace(day=1)
    rows=[record(month,'123.45')];docs={'supply':[],'purchasereturn':[]}
    history.save(month,rows,docs)
    file=history.archive(month);before=file.read_bytes()
    assert stat.S_IMODE(file.stat().st_mode)==0o600
    for sql in ['UPDATE supplier_purchase_month SET supply_sum=0','DELETE FROM supplier_purchase_month',
                "UPDATE supplier_purchase_period SET payload='{}'",'DELETE FROM supplier_purchase_period']:
        with pytest.raises(psycopg2.Error):
            with conn.cursor() as cur:cur.execute(sql)
        conn.rollback()
    with pytest.raises(ValueError,match='closed'):history.save(month,rows,docs)
    assert query('SELECT supply_sum FROM supplier_purchase_month')[0]['supply_sum']==Decimal('123.45')
    assert history.archive(month).read_bytes()==before


def test_current_month_refresh_is_atomic_and_cannot_be_closed(sandbox):
    conn,query=sandbox;month=history.today().replace(day=1);docs={'supply':[],'purchasereturn':[]}
    history.save(month,[record(month,'1.01')],docs)
    broken=record(month,'2.02');broken['supply_sum']='invalid'
    with pytest.raises(psycopg2.Error):history.save(month,[broken],docs)
    assert query('SELECT supply_sum FROM supplier_purchase_month')[0]['supply_sum']==Decimal('1.01')
    history.save(month,[],docs)
    assert query('SELECT count(*) n FROM supplier_purchase_month')[0]['n']==0
    assert query('SELECT closed_at FROM supplier_purchase_period')[0]['closed_at'] is None
    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:cur.execute('UPDATE supplier_purchase_period SET closed_at=now()')
    conn.rollback()


def test_purchase_api_reads_open_and_closed_periods_from_postgres(sandbox):
    conn,query=sandbox
    month=history.today().replace(day=1)
    previous=(month-dt.timedelta(days=1)).replace(day=1)
    docs={'supply':[],'purchasereturn':[]}
    history.save(previous,[record(previous,'1.01')],docs)
    history.save(month,[record(month,'2.02')],docs)
    tree=ast.parse((Path(__file__).resolve().parents[1]/'web/app.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='supplier_purchases_month')
    node.decorator_list=[]
    scope={'db':db}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'supplier-purchase-api','exec'),scope)
    result=scope['supplier_purchases_month']()
    assert result['months']==[previous.isoformat(),month.isoformat()]
    assert result['periods'][0]['closed_at'] is not None
    assert result['periods'][1]['closed_at'] is None
    assert result['current']==month.isoformat()
