# поток: prc
from decimal import Decimal
import pytest
from prices import assortment,loader,mailbox,catalog,unlinked
from prices.profiles import get_profile


@pytest.mark.parametrize('name',[
 '24" Игровой монитор ViewSonic XG2431 IPS Full HD',
 'A24 Воздушный фильтр (ELPAF24) для проекторов Epson',
 'Фильтр от дыма для EMP-6100', 'Картридж фильтрующий HEPA для очистителя',
 'МФУ лазерное HP LaserJet с тонером', 'Принтер Epson L805 с чернилами',
 'Экран моторизованный для проектора','Фотобумага Epson A4',
 'Ролик захвата бумаги для принтера','Магнитный вал HP','Ракель HP','HP магнитный вал картриджа',
 'HP чип картриджа W1106A','Сервисный комплект для проектора Epson',
 'Чип HP для картриджа W1106A','Промывочная жидкость для Canon',
 'Перезаправляемый картридж Epson','Произвольный товар ABC-42'])
def test_outside_scope_does_not_become_a_new_cartridge(name):
 assert assortment.name_reason(name)


@pytest.mark.parametrize('name',[
 'Картридж HP W1106A для лазерного принтера без чипа',
 'Картридж Epson для МФУ','Лазерный картридж Canon с чипом','HP laser printer cartridge CF283A','Картридж HP с новым магнитным валом',
 'Фотобарабан Ricoh для принтера','Чернила Epson сублимационные 1000мл',
 'Тонер Cactus 500гр для принтера','Твердые чернила Xerox',
 'Комплект картриджей Brother','Бункер отработанного тонера Xerox',
 'Контейнер для отработанных чернил Epson','C9345 (C12C934591) Eмкость для отработанных чернил','Емкость для Epson C13T671600 (T6716/PXMB8)','Девелопер Ricoh',
 'Краска полиграфическая Ricoh','Лента для принтера Epson','Печатающая головка Canon',
 'СНПЧ для Epson','Фьюзерное масло Ricoh','Заправочный комплект HP',
 'Комплект для заправки HP','Блок проявки Kyocera','Сервисный комплект HP',
 'Комплект чернил Canon','Комплект фотобарабанов Ricoh','Блок переноса Kyocera',
 'Фотобарабан + картридж HP','Термопленка HP','Лента Brother TZe для печати наклеек',
 'FK-170 Sakura узел термозакрепления для Kyocera',
 'Набор для печати Canon KP-108IN (фотобумага + цветные картриджи)'])
def test_existing_printer_consumable_types_remain_allowed(name):
 assert assortment.name_reason(name) is None


def test_supplier_category_exclusions_remain_authoritative():
 p=get_profile('kaktus_msk')
 assert assortment.row_reason({'name':'Тонер HP','group':'Материалы для заправки','subgroup':'Тонер'},p) is None
 assert assortment.row_reason({'name':'Картридж HP','group':'Оргтехника','subgroup':'Принтеры'},p)
 assert assortment.row_reason({'name':'Барабан OPC','group':'Материалы для заправки','subgroup':'Барабаны'},p)
 assert assortment.row_reason({'name':'Лента Brother TZe','group':'Совместимые для Brother','subgroup':'Ленты'},p) is None


def test_offscope_is_removed_before_any_ms_lookup(monkeypatch):
 def forbidden(*a,**kw):raise AssertionError('MS lookup on equipment')
 monkeypatch.setattr(loader,'lookup_by_article',forbidden)
 rows=[{'name':'Игровой монитор ViewSonic','article':'1','qty':5},
       {'name':'Воздушный фильтр Epson','article':'2','qty':2}]
 ready,off=loader.classify(rows,get_profile('sakura'),Decimal(1))
 assert not ready and len(off)==2 and all(r['reason']=='category_off' for r in off)


def test_same_day_attachment_choice_counts_allowed_goods_not_all_stock(monkeypatch):
 equipment=[{'name':'Игровой монитор','qty':100} for _ in range(50)]
 carts=[{'name':'Картридж HP','qty':1} for _ in range(2)]
 monkeypatch.setattr(__import__('prices.parser',fromlist=['parse']),'parse',lambda content,p:(equipment if content==b'bad' else carts,1))
 bad={'date':'Fri, 06 Nov 2026 10:00:00 +0000','content':b'bad'}
 good={'date':'Fri, 06 Nov 2026 09:00:00 +0000','content':b'good'}
 assert mailbox._pick_by_content([bad,good],get_profile('sakura')) is good
 assert bad['pick_rows']==0 and good['pick_rows']==2
 good['date']='Thu, 05 Nov 2026 09:00:00 +0000'
 assert mailbox._pick_by_content([bad,good],get_profile('sakura')) is bad


def test_novelty_sync_cannot_bypass_source_gate(monkeypatch):
 def forbidden(*a,**kw):raise AssertionError('equipment reached catalogue matcher')
 monkeypatch.setattr(catalog,'analyze',forbidden);monkeypatch.setattr(catalog,'save',forbidden)
 assert catalog.sync([{'name':'Воздушный фильтр Epson','article':'F','price':10}], 'sakura')==(0,0)


def test_unlinked_live_ms_queue_excludes_equipment_without_modifying_ms(monkeypatch):
 docs=[{'id':'doc','name':'test'}]
 monkeypatch.setattr(unlinked,'enters',lambda s:docs)
 monkeypatch.setattr(unlinked,'current',lambda d:[('sakura','2026-11-06',docs[0])])
 monkeypatch.setattr(unlinked,'positions',lambda d:[{'assortment':{'meta':{'href':'x/'+k}},'quantity':1} for k in ['bad','good']])
 monkeypatch.setattr(unlinked,'cards',lambda ids:{'bad':{'name':'Игровой монитор','externalCode':'auto'},'good':{'name':'Картридж HP','externalCode':'auto'}})
 rows=unlinked.collect()
 assert [r['ms_id'] for r in rows]==['good']


def test_local_cleanup_archives_records_and_suggestions_and_is_repeatable(monkeypatch):
 from contextlib import contextmanager
 from pathlib import Path
 from uuid import uuid4
 from core import db
 from prices.assortment_reconcile import reconcile
 original=db.get_conn;schema='assortment_archive_test_'+uuid4().hex
 with original() as c,c.cursor() as q:
  q.execute(f'CREATE SCHEMA "{schema}"');q.execute(f'SET LOCAL search_path TO "{schema}"')
  q.execute('''CREATE TABLE prc_novelty(id int PRIMARY KEY,supplier_key text,article text,name text,decision text);
   CREATE TABLE prc_novelty_candidate(novelty_id int REFERENCES prc_novelty(id) ON DELETE CASCADE,ms_code text);
   CREATE TABLE prc_unlinked(ms_id text PRIMARY KEY,supplier_key text,article text,name text,decision text);
   CREATE TABLE prc_unlinked_candidate(ms_id text REFERENCES prc_unlinked(ms_id) ON DELETE CASCADE,cand_code text);''')
  q.execute((Path(__file__).parents[1]/'migrations/419_prc_assortment_archive.sql').read_text())
 @contextmanager
 def isolated():
  with original() as c:
   with c.cursor() as q:q.execute(f'SET LOCAL search_path TO "{schema}"')
   yield c
 monkeypatch.setattr(db,'get_conn',isolated)
 try:
  db.execute("INSERT INTO prc_novelty VALUES(1,'sakura','F','Воздушный фильтр','pending'),(2,'sakura','K','Картридж HP','pending')")
  db.execute("INSERT INTO prc_novelty_candidate VALUES(1,'0001')")
  db.execute("INSERT INTO prc_unlinked VALUES('bad','blossom','P','Наклейка адресная А4','pending'),('good','sakura','C','Емкость для Epson C13T671600','pending')")
  result=reconcile();assert result['novelty']['removed']==1 and result['unlinked']['removed']==1
  assert db.query('SELECT id FROM prc_novelty')==[{'id':2}]
  assert db.query('SELECT ms_id FROM prc_unlinked')==[{'ms_id':'good'}]
  archived=db.query('SELECT row_data,candidates FROM prc_assortment_excluded ORDER BY id')
  assert archived[0]['row_data']['decision']=='pending'
  assert archived[0]['candidates']==[{'novelty_id':1,'ms_code':'0001'}]
  assert all(x['removed']==0 for x in reconcile().values())
  assert db.query('SELECT count(*) n FROM prc_assortment_excluded')[0]['n']==2
 finally:
  with original() as c,c.cursor() as q:q.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_supplier_category_evidence_is_preserved_at_novelty_ingress(monkeypatch):
 seen=[]
 monkeypatch.setattr(catalog,'analyze',lambda rows,*args:seen.extend(rows) or {})
 monkeypatch.setattr(catalog,'save',lambda *args:0)
 row={'article':'NEW','name':'OEM12345','price':100,'group':'Совместимые для HP','subgroup':'Картриджи'}
 assert catalog.sync([row],'kaktus_msk')==(0,0)
 assert seen==[row]
 seen.clear()
 assert catalog.sync([dict(row,group='Оргтехника',name='Картридж HP')],'kaktus_msk')==(0,0)
 assert not seen
 assert catalog.sync([dict(row,name='Игровой монитор')],'kaktus_msk')==(0,0)
 assert not seen
