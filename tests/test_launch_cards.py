# поток: prc
from contextlib import contextmanager
from datetime import datetime,date,timezone,timedelta
from pathlib import Path
from uuid import uuid4
import pytest
from core import db
from collectors import launch_cards as cards

@pytest.mark.parametrize('offer,name,role,qty',[
 ('7205','','main',None),('72051','','printer',None),
 ('7205X10','','bundle',10),('7205C6SIXW5M','','main',None),
 ('6452X3MS22I0','Комплект картриджей 3 шт.','bundle',3),
 ('6452X3MS22I0','Картридж','unknown',None),
 ('23532M9MWDZH','Лента для принтера Brother','printer',None),
 ('23532M9MWDZH','Картридж для принтеров','unknown',None),
 ('19333del','','unknown',None)])
def test_confirmed_roles_and_ambiguous_random_suffixes(offer,name,role,qty):
    result=cards.classify(offer,name,'oz_acc2')
    assert result[1:3]==(role,qty)


def test_exact_title_supports_diskver_alias_but_conflicting_title_does_not(monkeypatch):
    source=[dict(offer_id='7205',name='Фотобарабан Brother'),
            dict(offer_id='720589Q3LQDJ',name='Фотобарабан DS Brother')]
    monkeypatch.setattr(cards.db,'query',lambda *a:source)
    hints=cards.role_hints('oz_acc2')
    assert cards.classify(source[1]['offer_id'],source[1]['name'],'oz_acc2',hints)[1]=='main'
    source.append(dict(offer_id='72051',name='Фотобарабан Brother'))
    assert '720589Q3LQDJ' not in cards.role_hints('oz_acc2')


def test_new_observations_and_source_recovery_do_not_invent_historical_dates():
    today=date(2026,11,8)
    assert cards.observation_dates(None,'selling',today,True)==(None,None,True)
    assert cards.observation_dates(None,'selling',today,False)==(today,today,False)
    old=dict(card_at=None,active_at=None,seeded=True,present=True,state='card')
    assert cards.observation_dates(old,'selling',today,True)==(None,None,True)
    assert cards.observation_dates(old,'selling',today,False)==(None,today,True)


@pytest.fixture
def isolated(monkeypatch):
    original=db.get_conn;schema='launch_cards_test_'+uuid4().hex
    with original() as c,c.cursor() as q:
        q.execute(f'CREATE SCHEMA "{schema}"');q.execute(f'SET LOCAL search_path TO "{schema}"')
        q.execute(Path('migrations/418_prc_novelties_recovery.sql').read_text())
        q.execute(Path('migrations/420_prc_launch_cards.sql').read_text())
        q.execute("INSERT INTO prc_launch_source_state(showcase,status) VALUES('oz_acc1','ok')")
        q.execute("INSERT INTO prc_launch_mp(external_code,showcase,state) VALUES('7205','oz_acc1','selling')")
    @contextmanager
    def connect():
        with original() as c:
            with c.cursor() as q:q.execute(f'SET LOCAL search_path TO "{schema}"')
            yield c
    monkeypatch.setattr(db,'get_conn',connect);monkeypatch.setattr(cards,'role_hints',lambda *a:{})
    yield
    with original() as c,c.cursor() as q:q.execute(f'DROP SCHEMA "{schema}" CASCADE')


def record(offer,state='selling',card_id=None):
    return dict(offer=offer,state=state,card_id=card_id or offer,name='Картридж',note='')


def test_active_bundle_never_replaces_main_and_each_variant_remains_visible(isolated):
    def save(*rows):cards.persist_source('oz_acc1',{'7205':dict(variants=list(rows))})
    save(record('7205X2'))
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='none'
    save(record('7205','card'),record('7205X2'))
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='card'
    save(record('7205'),record('72051','card'),record('7205X2'))
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='selling'
    summary=cards.summaries(['7205'],{'oz_acc1':{'status':'ok'}})['7205']['oz_acc1']
    assert summary['main']['active']==1 and summary['printer']['card']==1 and summary['bundle']['active']==1
    page=cards.card_page('7205','oz_acc1','printer')
    assert page['total']==1 and page['cards'][0]['offer']=='72051'
    save(record('7205X2'))
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='none'
    assert db.query('SELECT count(*) n FROM prc_launch_card')[0]['n']==3


def test_unknown_role_or_source_is_not_reported_as_absent(isolated):
    cards.persist_source('oz_acc1',{'7205':dict(variants=[record('720589Q3LQDJ')])})
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='unknown'
    cards.persist_source('oz_acc1',{'7205':dict(variants=[record('7205')])})
    db.execute("UPDATE prc_launch_source_state SET status='error'")
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='unknown'
    assert cards.card_page('7205','oz_acc1')['cards'][0]['state']=='unknown'
    assert cards.summaries(['7205'],{'oz_acc1':{'status':'error'}})['7205']['oz_acc1']['main']['unknown']==1


def test_wb_duplicate_vendor_codes_keep_distinct_card_ids_and_repeat_is_idempotent(isolated):
    rows={'7205':dict(variants=[record('7205',card_id='111'),record('7205','card',card_id='222')])}
    cards.persist_source('wb_acc1',rows);cards.persist_source('wb_acc1',rows)
    assert db.query("SELECT count(*) n FROM prc_launch_card WHERE showcase='wb_acc1'")[0]['n']==2
    assert all(r['active_at'] is None for r in db.query('SELECT active_at FROM prc_launch_card'))


def test_ozon_collector_retains_inactive_main_beside_active_bundle(monkeypatch):
    from collectors import launch_track
    monkeypatch.setattr(launch_track,'ozon_visible',lambda account:{'7205X2'})
    def query(sql,*params):
        if 'to_regclass' in sql:return [{'r':None}]
        return [dict(offer_id='7205',is_archived=True,name='Картридж'),
                dict(offer_id='7205X2',is_archived=False,name='Комплект 2 шт.')]
    monkeypatch.setattr(launch_track.db,'query',query)
    result=launch_track.state_ozon('oz_acc1')['7205']
    assert result['state']=='selling'  # Old model aggregate remains a separate historical layer.
    assert {v['offer']:v['state'] for v in result['variants']}=={'7205':'card','7205X2':'selling'}


def test_successful_empty_snapshot_means_absence_but_no_snapshot_means_unknown(isolated):
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='unknown'
    cards.persist_source('oz_acc1',{})
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='none'


def test_old_any_variant_dates_are_not_reused_for_main(isolated):
    db.execute("UPDATE prc_launch_mp SET card_at='2026-07-01',selling_at='2026-07-02',seeded=false")
    cards.persist_source('oz_acc1',{'7205':dict(variants=[record('7205')])})
    row=db.query('SELECT state,card_at,selling_at FROM prc_launch_primary')[0]
    assert row==dict(state='selling',card_at=None,selling_at=None)
    assert db.query('SELECT selling_at FROM prc_launch_mp')[0]['selling_at']==date(2026,7,2)


def test_review_queue_includes_inactive_derivative_even_when_main_is_active(isolated):
    rows={'7205':dict(variants=[record('7205'),record('72051','card'),record('7205X2')])}
    cards.persist_source('oz_acc1',rows)
    assert db.query('SELECT state FROM prc_launch_primary')[0]['state']=='selling'
    assert '7205' in cards.problem_codes()
    rows['7205']['variants'][1]['state']='selling'
    cards.persist_source('oz_acc1',rows)
    assert '7205' not in cards.problem_codes()


def test_printer_alias_can_be_confirmed_with_same_title_and_appended_yield(monkeypatch):
    data=[dict(offer_id='6947',name='Картридж W9017MC для принтеров HP MFP E52545, MPS Solutions LaserJet E50045 черный'),
          dict(offer_id='69471',name='Картридж для HP MPS Solutions LaserJet E50045'),
          dict(offer_id='69471JQYQ9CX',name='Картридж для HP MPS Solutions LaserJet E50045 22500 страниц')]
    monkeypatch.setattr(cards.db,'query',lambda *a:data)
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('69471JQYQ9CX','', 'wb_acc2',hints)[1]=='printer'
    assert cards.normalized_title('Картридж HP 50045 22500 страниц')=='картридж hp 50045'
    assert cards.normalized_title('Комплект (3 шт.) 100 мл')=='комплект (3 шт.) 100 мл'


@pytest.mark.parametrize('offer,index,device',[
 ('68973UVTVFB9','3','MF662Cdw'),('68974JJUGIHA','4','MF665Cdw'),('68975SKXBH2C','5','MF667Cdw')])
def test_wb_only_alias_uses_canonical_title_without_ozon_alias(monkeypatch,offer,index,device):
    title='Картридж для Canon i-SENSYS '+device
    source=[dict(offer_id='6897',name='Картридж 075H для принтеров Canon'),
            dict(offer_id='6897'+index,name=title)]
    monkeypatch.setattr(cards.db,'query',lambda *a:source)
    hints=cards.role_hints('wb_acc2')
    assert offer not in hints
    assert cards.classify(offer,title+' 3500 страниц','wb_acc2',hints)[1]=='printer'
    assert cards.classify(offer.replace('6897','6997'),title,'wb_acc2',hints)[1]=='printer'


def test_title_fallback_rejects_conflicts_and_does_not_reuse_inferred_roles(monkeypatch):
    title='Картридж для Canon i-SENSYS MF662Cdw'
    source=[dict(offer_id='6897',name=title),dict(offer_id='68973',name=title)]
    monkeypatch.setattr(cards.db,'query',lambda *a:source)
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('68973UVTVFB9',title+' 3500 страниц','wb_acc2',hints)[1]=='unknown'
    assert cards.classify('6897ABC12345',title,'wb_acc2',hints)[1]=='main'
    vague='Товар для Canon MF662Cdw'
    source[:]=[dict(offer_id='68973UVTVFB9',name=vague,role='printer')]
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('68974JJUGIHA',vague,'wb_acc2',hints)[1]=='unknown'


def test_wb_only_ribbon_alias_uses_print_yield_in_characters(monkeypatch):
    monkeypatch.setattr(cards.db,'query',lambda *a:[dict(offer_id='52161',name='Картридж для Epson PLQ-20')])
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('52161ECJ4WRX','Картридж для Epson PLQ-20 15000000 знаков','wb_acc2',hints)[1]=='printer'
    assert cards.classify('52161ECJ4WRX','Картридж для Epson PLQ-30 15000000 знаков','wb_acc2',hints)[1]=='printer'


@pytest.mark.parametrize('offer,title',[
 ('67324NQ0850N','Заправочный комплект для HP DeskJet 3637 60мл.'),
 ('11921Z0H573N','Картридж для HP LJ Pro M501 9000 страниц'),
 ('22033YG82BX9','Картридж для Epson ERC-38 2250000 знаков'),
 ('56021T8SIEKE','Картридж для Катюша D320 24000 страниц'),
 ('57542OMKG1ZI','Тонер-картридж для Bulat TK-101A 2000 страниц'),
 ('68973UVTVFB9','Картридж для Canon i-SENSYS MF662Cdw 3500 страниц')])
def test_specific_device_template_works_without_reference_card(offer,title):
    assert cards.classify(offer,title,'wb_acc2')[1:3]==('printer',None)


@pytest.mark.parametrize('title',[
 'Картридж для Canon',
 'Картридж для Canon MF662Cdw, MF665Cdw',
 'Картридж для Canon MF660-MF670',
 'Картридж для принтеров Canon MF662Cdw MF665Cdw',
 'Картридж 075H для Canon MF662Cdw',
 'Картридж для Epson ERC-38 3 шт.',
 'Картридж для прогулок 500'])
def test_generic_multi_device_and_quantity_titles_are_not_specific_printers(title):
    assert cards.classify('68973UVTVFB9',title,'wb_acc2')[1]=='unknown'


def test_random_x1_tail_does_not_create_a_single_item_bundle():
    result=cards.classify('5636X1FLKT72','Картридж DS T08 для Canon X 1238i, X 1238iF черный','wb_acc2')
    assert result[1:3]==('main',None)


def test_nonnumeric_device_needs_same_model_reference_and_measure_does_not_change_qty(monkeypatch):
    title='Картридж для HP DesignJet Studio Wood'
    monkeypatch.setattr(cards.db,'query',lambda *a:[dict(offer_id='48373',name=title)])
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('48373CFOLOIH',title+' 80мл.','wb_acc2',hints)[1:3]==('printer',None)
    assert cards.classify('49373CFOLOIH',title+' 80мл.','wb_acc2',hints)[1]=='unknown'
    assert cards.normalized_title('Комплект (3 шт.) 100 мл')=='комплект (3 шт.) 100 мл'
    assert cards.classify('4837X3','Комплект (3 шт.) 100 мл','wb_acc2',hints)[1:3]==('bundle',3)


def test_saved_wb_alias_with_single_device_does_not_bypass_conflicting_references(monkeypatch):
    title='Картридж для Canon i-SENSYS MF662Cdw'
    source=[dict(offer_id='6897',name=title),dict(offer_id='68973',name=title),
            dict(offer_id='68973UVTVFB9',name=title+' 3500 страниц')]
    monkeypatch.setattr(cards.db,'query',lambda *a:source)
    hints=cards.role_hints('wb_acc2')
    assert cards.classify('68973UVTVFB9',title+' 3500 страниц','wb_acc2',hints)[1]=='unknown'
