# поток: fin
import csv
import io
import json
from datetime import date
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4
import pytest
from core import db
from reports import fts_report as report, fts_form as form, fts_page as page
from collectors import ozon_orders_report as collector

PERIOD = date(2026,10,1)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    original = db.get_conn
    schema = 'test_fts_' + uuid4().hex
    with original() as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE SCHEMA ' + schema)
            for table in ('raw_ozon_orders_report','raw_ozon_posting','fts_posting_status'):
                cur.execute(f'CREATE TABLE {schema}.{table} (LIKE public.{table} INCLUDING DEFAULTS)')
    @contextmanager
    def connection():
        with original() as conn:
            with conn.cursor() as cur:
                cur.execute('SET search_path TO ' + schema)
            yield conn
    monkeypatch.setattr(db, 'get_conn', connection)
    monkeypatch.setattr(report, 'BASE_DIR', tmp_path)
    folder=tmp_path/'.private/fts_sources';folder.mkdir(parents=True)
    for account in report.ORGS:
        (folder/f'2026-10_{account}.json').write_text(json.dumps(dict(status='complete',schemas=['fbs','fbo'])))
    yield folder
    with original() as conn:
        with conn.cursor() as cur:cur.execute('DROP SCHEMA ' + schema + ' CASCADE')


def insert(account, cluster, buyout=True, scheme='fbs'):
    db.execute("INSERT INTO raw_ozon_orders_report(account,posting_number,delivery_schema,status,delivered_at,is_buyout,payload) VALUES (%s,'same',%s,'Доставлен','2026-10-31',%s,'{}')",(account,scheme,buyout))
    db.execute("INSERT INTO raw_ozon_posting(account,posting_number,scheme,payload,period_from,period_to) VALUES (%s,'same',%s,%s,'2026-10-01','2026-10-31')",(account,scheme,json.dumps({'financial_data':{'cluster_to':cluster}})))


def test_country_join_keeps_accounts_and_schemes_separate(isolated):
    insert('oz_acc1','Беларусь')
    insert('oz_acc2','Москва')
    db.execute("INSERT INTO raw_ozon_posting(account,posting_number,scheme,payload,period_from,period_to) VALUES ('oz_acc1','same','fbo','{\"financial_data\":{\"cluster_to\":\"Алматы\"}}','2026-10-01','2026-10-31')")
    result=report.build(PERIOD)
    assert result['totals']['eaes']==1
    assert result['rows'][0]['country']=='BY'
    assert result['totals']['due']==0
    assert not result['source_warnings']


def test_empty_source_is_incomplete_not_verified_zero(isolated):
    (isolated/'2026-10_oz_acc2.json').unlink()
    assert report.build(PERIOD)['source_warnings']
    assert 'Данные неполные' in page.html_for(PERIOD)
    with pytest.raises(ValueError,match='неполны'):report.csv_for(PERIOD)


def test_missing_delivery_country_cannot_be_silently_skipped(isolated):
    insert('oz_acc1',None)
    assert 'Не определена страна' in ' '.join(report.build(PERIOD)['source_warnings'])


def test_csv_preserves_buyout_evidence(isolated):
    insert('oz_acc1','Армения')
    text=report.csv_for(PERIOD).decode('utf-8-sig')
    rows=list(csv.reader(io.StringIO(text),delimiter=';'))
    assert len(rows)==2 and rows[1][3]=='AM'
    assert rows[1][8:10]==['Да','Нет']


def test_demand_lookup_requires_actual_account_org(monkeypatch):
    captured=[]
    monkeypatch.setattr(db,'query',lambda sql,params=None:captured.append((sql,params)) or [])
    assert report._posting_goods('same',[],0,0,'oz_acc2')[1]=='нет отгрузки в МойСкладе'
    assert 'org=%s' in captured[0][0]
    assert captured[0][1]==('same',report.ORGS['oz_acc2'])


@pytest.mark.parametrize('case',['mixed','digital','incomplete','bad_goods','already_filed'])
def test_xml_rejects_unsafe_exports(case):
    row=dict(account='oz_acc2',status='due',goods=[{'ved':'8443999000'}],problems=None)
    data=dict(by_country={'KZ':dict(rows=[row])},source_warnings=[])
    if case=='mixed':data['by_country']['KZ']['rows'].append({**row,'account':'oz_acc1'})
    if case=='digital':row['account']='oz_acc1'
    if case=='incomplete':data['source_warnings']=['Нет страны']
    if case=='bad_goods':row['problems']='нет ГТД'
    if case=='already_filed':row['status']='filed'
    with pytest.raises(ValueError):form.make_xml(PERIOD,'KZ',data)


def test_parser_rejects_missing_buyout_column():
    with pytest.raises(RuntimeError,match='buyout'):
        collector._rows_from_csv('Номер отправления;Статус\nsame;Доставлен\n'.encode())


def test_download_http_error_is_not_treated_as_empty(monkeypatch):
    class Response:
        content=b'<Error>AccessDenied</Error>'
        def raise_for_status(self):raise RuntimeError('403')
    class Good:
        def raise_for_status(self):pass
        def json(self):return {'result':{'code':'test','status':'success','file':'test'}}
    monkeypatch.setattr(collector.requests,'post',lambda *a,**kw:Good())
    monkeypatch.setattr(collector.requests,'get',lambda *a,**kw:Response())
    monkeypatch.setattr(collector.time,'sleep',lambda n:None)
    with pytest.raises(RuntimeError,match='403'):
        collector._request_report({},'fbs',PERIOD,PERIOD)


def test_xml_keeps_per_delivery_usd_rates_and_account(monkeypatch):
    from decimal import Decimal
    import xml.etree.ElementTree as ET
    monkeypatch.setattr(form.cbr,'rub_to_usd',lambda amount,day: Decimal(amount)/(10 if day.day==1 else 20))
    goods=dict(ved='8443999000',gtd='10228020/030226/5014219',origin='Китай',origin_code='CN',description='Товар',weight_kg=Decimal('1'),amount_rub=Decimal('100'))
    rows=[dict(account='oz_acc2',status='due',goods=[goods],problems=None,amount_rub=Decimal('100'),posting_number=f'order{day}',delivered_at=date(2026,10,day)) for day in (1,2)]
    data=dict(by_country={'KZ':dict(rows=rows,city='Алматы')},source_warnings=[])
    raw=form.make_xml(PERIOD,'KZ',data,account='oz_acc2')
    root=ET.fromstring(raw)
    values=lambda name:[e.text for e in root.iter() if e.tag.endswith('}'+name)]
    assert values('ReportingDate')==['2026-10']
    assert values('StatisticalCostUSD')==['15']
    assert values('InvoicedCost')==['200']
    assert values('NetWeightQuantity')==['2']
    assert values('INN')==['7811803918']*3


def test_completed_checkpoint_does_not_hide_lost_source_rows(isolated):
    (isolated/'2026-10_oz_acc1.json').write_text(json.dumps(dict(status='complete',schemas=['fbs','fbo'],count=100)))
    assert 'отсутствует' in ' '.join(report.build(PERIOD)['source_warnings'])
