# поток: fin
import base64
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
import xml.etree.ElementTree as ET

import openpyxl
import pytest
from core import db
from collectors import wb_documents as collector
from reports import upd_form as form, upd_page as page


def position(**changes):
    return dict(n=1,article='0012',name='Картридж "А" & Б',qty='2',sum_with_vat='105.00',vat_rate='22%',vat_sum='18.93',**changes)


def document(**changes):
    return dict(account='wb_acc2',doc_number='123',doc_date=date(2026,10,26),
                total_with_vat=Decimal('105'),positions=[position()],**changes)


def archive(total=105, tax=18.93):
    workbook=openpyxl.Workbook(); sheet=workbook.active
    sheet.append(['Уведомление']);sheet.append([''])
    sheet.append(['№','Артикул','Наименование','Количество','Сумма выкупа вкл. НДС','Ставка НДС','Сумма НДС'])
    sheet.append([1,'0012','Картридж',2,105,'22%',18.93])
    sheet.append(['Итого',None,None,None,total,None,tax])
    stream=io.BytesIO();workbook.save(stream)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:z.writestr('notice.xlsx',stream.getvalue())
    return output.getvalue()


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    original=db.get_conn;schema='test_upd_'+uuid4().hex
    with original() as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE SCHEMA '+schema)
            cur.execute(f'CREATE TABLE {schema}.wb_redeem_notice (LIKE public.wb_redeem_notice INCLUDING ALL)')
    @contextmanager
    def connection():
        with original() as conn:
            with conn.cursor() as cur:cur.execute('SET search_path TO '+schema)
            yield conn
    monkeypatch.setattr(db,'get_conn',connection)
    monkeypatch.setattr(collector,'STATE_DIR',tmp_path)
    monkeypatch.setattr(page,'STATE_DIR',tmp_path)
    yield tmp_path
    with original() as conn:
        with conn.cursor() as cur:cur.execute('DROP SCHEMA '+schema+' CASCADE')


def insert(account, number='123', day=date(2026,10,26), status='due'):
    db.execute("""INSERT INTO wb_redeem_notice(account,doc_number,doc_date,service_name,total_wo_vat,total_vat,total_with_vat,positions,upd_status)
      VALUES (%s,%s,%s,'redeem-notification-123',86.07,18.93,105,%s,%s)""",
      (account,number,day,json.dumps([position()]),status))


def token(scope,exp=9999999999):
    return 'header.'+base64.urlsafe_b64encode(json.dumps(dict(s=scope,exp=exp)).encode()).decode().rstrip('=')+'.signature'


def test_token_fallback_requires_documents_scope_and_expiry(monkeypatch):
    for names in collector.TOKEN_ENV.values():
        for key in names:monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('WB_TOKEN_DOCS_ACC1',token(1<<12,exp=1))
    monkeypatch.setenv('WB_TOKEN_PRICES_ACC1',token(1<<3))
    good=token(1<<12);monkeypatch.setenv('WB_TOKEN_FEEDBACK_ACC1',good)
    assert collector._token('wb_acc1')==good
    with pytest.raises(RuntimeError):collector._token('wb_acc2')


def test_list_follows_pagination_without_losing_51st_document(monkeypatch,tmp_path):
    calls=[]
    def get(account,method,params):
        calls.append(params['offset'])
        return {'data':{'documents':[dict(serviceName=f'doc{i}') for i in range(params['offset'],min(51,params['offset']+50))]}}
    monkeypatch.setattr(collector,'_get',get);monkeypatch.setattr(collector,'STATE_DIR',tmp_path)
    assert len(collector.list_notices('wb_acc1',date(2026,1,1),date(2026,11,9)))==51
    assert calls==[0,50]


def test_list_rejects_repeating_pages(monkeypatch,tmp_path):
    monkeypatch.setattr(collector,'STATE_DIR',tmp_path)
    monkeypatch.setattr(collector,'_get',lambda *a:{'data':{'documents':[dict(serviceName=f'doc{i}') for i in range(50)]}})
    with pytest.raises(RuntimeError,match='пагинация'):collector.list_notices('wb_acc1',date(2026,1,1),date(2026,11,9))


def test_download_archive_is_reusable_without_another_api_call(monkeypatch,tmp_path):
    calls=[];raw=archive()
    monkeypatch.setattr(collector,'STATE_DIR',tmp_path)
    monkeypatch.setattr(collector,'_get',lambda *a:calls.append(a) or {'data':{'document':base64.b64encode(raw).decode()}})
    assert collector.download('wb_acc1','redeem-notification-123')==raw
    assert collector.download('wb_acc1','redeem-notification-123')==raw
    assert len(calls)==1
    with pytest.raises(RuntimeError):collector.download('wb_acc1','../123')


def test_parser_preserves_leading_zero_and_source_totals():
    positions,totals=collector.parse_zip(archive())
    assert positions[0]['article']=='0012' and positions[0]['qty']=='2'
    assert totals==(Decimal('105'),Decimal('18.93'))


def fake_sources(monkeypatch,raw):
    monkeypatch.setattr(collector,'list_notices',lambda *a:[dict(name='Уведомление о выкупе №123 от 2026-10-26',serviceName='redeem-notification-123')])
    monkeypatch.setattr(collector,'download',lambda *a:raw)


def test_reimport_does_not_change_manual_status_or_notifications(isolated,monkeypatch):
    insert('wb_acc1',status='made')
    db.execute("UPDATE wb_redeem_notice SET upd_made_at='2026-11-01',sent_at='2026-11-02'")
    before=db.query('SELECT upd_status,upd_made_at,sent_at FROM wb_redeem_notice')[0]
    fake_sources(monkeypatch,archive())
    collector.collect('wb_acc1',date(2026,10,1),date(2026,11,9),force=True)
    assert db.query('SELECT upd_status,upd_made_at,sent_at FROM wb_redeem_notice')[0]==before
    assert json.loads((isolated/'wb_acc1.json').read_text())['status']=='complete'


def test_gross_discrepancy_blocks_loading_and_complete_checkpoint(isolated,monkeypatch):
    fake_sources(monkeypatch,archive(total=106))
    with pytest.raises(RuntimeError,match='сумма позиций'):collector.collect('wb_acc1',date(2026,10,1),date(2026,11,9))
    assert not db.query('SELECT * FROM wb_redeem_notice')
    assert json.loads((isolated/'wb_acc1.json').read_text())['status']!='complete'


def test_cent_tax_rounding_keeps_header_tax_as_source(isolated,monkeypatch):
    fake_sources(monkeypatch,archive(tax=18.94))
    collector.collect('wb_acc1',date(2026,10,1),date(2026,11,9))
    assert db.query('SELECT total_vat FROM wb_redeem_notice')[0]['total_vat']==Decimal('18.94')


@pytest.mark.parametrize('account,day,expected',[('wb_acc1',date(2026,1,1),'5.00'),('wb_acc2',date(2026,7,31),'0'),('wb_acc2',date(2026,8,1),'5.00')])
def test_existing_vat_policy_does_not_change_gross(account,day,expected):
    doc=document();doc['doc_date']=day
    values=form.amounts(account,doc)[0]
    assert values['vat']==Decimal(expected)
    assert values['total']==105 and values['base']+values['vat']==105


@pytest.mark.parametrize('account',['wb_acc1','wb_acc2'])
def test_xml_and_page_share_vat_and_correct_organization(isolated,account):
    insert(account)
    name,raw=form.make_upd(account,'123',now=datetime(2026,11,9,12,0))
    root=ET.fromstring(raw)
    seller=root.find('.//СвПрод//СвЮЛУч')
    assert seller.attrib['ИННЮЛ']==form.SELLERS[account]['inn']
    assert root.find('.//СвСчФакт').attrib['ДатаДок']=='26.10.2026'
    assert root.find('.//СведТов').attrib['НалСт']=='5%'
    assert root.find('.//ВсегоОпл').attrib['СтТовУчНалВсего']=='105.00'
    assert root.find('.//СумНалВсего/СумНал').text=='5.00'
    assert form.SELLERS[account]['edi_id'] in name
    rows=page._rows(date(2026,10,1))
    assert rows[0]['total_vat']==Decimal('5') and rows[0]['total_wo_vat']==Decimal('100')


def test_month_filter_and_same_document_number_do_not_mix_accounts(isolated):
    insert('wb_acc1');insert('wb_acc2',day=date(2026,11,1))
    assert [r['account'] for r in page._rows(date(2026,10,1))]==['wb_acc1']
    assert len(page._rows())==2
    assert form.notice('wb_acc2','123')['doc_date']==date(2026,11,1)


@pytest.mark.parametrize('qty',['0','NaN','-1'])
def test_invalid_quantity_blocks_export_but_keeps_page_available(isolated,qty):
    insert('wb_acc1')
    positions=[position()];positions[0]['qty']=qty
    db.execute('UPDATE wb_redeem_notice SET positions=%s',(json.dumps(positions),))
    with pytest.raises((RuntimeError,ArithmeticError)):form.make_upd('wb_acc1','123')
    assert page._rows()[0]['problem']
    assert 'Скачать УПД' not in page.html_for()


def test_missing_sources_are_explicit(isolated):
    text=page.html_for(date(2026,10,1))
    assert 'итоги предварительные' in text
    assert '10.2026' in text


def test_unknown_historical_status_is_not_claimed_as_pending_or_sent(isolated):
    insert('wb_acc1',status='unknown')
    text=page.html_for()
    assert 'проверить выгрузку в Диадоке' in text
    assert 'отметка выгрузки не сохранилась' in text
    assert 'УПД к выгрузке</div><div class="v">0</div>' in text
    assert 'Выгружено</div><div class="v">0</div>' in text


def test_lost_history_is_visible_even_after_a_narrow_refresh(isolated):
    insert('wb_acc1')
    for account in ('wb_acc1','wb_acc2'):
        (isolated/f'{account}.json').write_text(json.dumps(dict(status='complete',listed=0,stored_count=10,completed_at=datetime.now(timezone.utc).isoformat())))
    warnings=page.source_warnings()
    assert sum('отсутствует' in warning for warning in warnings)==2


def test_large_tax_discrepancy_is_not_accepted_as_rounding(isolated,monkeypatch):
    fake_sources(monkeypatch,archive(tax=20))
    with pytest.raises(RuntimeError,match='НДС строк'):collector.collect('wb_acc1',date(2026,10,1),date(2026,11,9))
    assert not db.query('SELECT * FROM wb_redeem_notice')
