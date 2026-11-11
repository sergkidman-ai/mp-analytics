# поток: prc
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4
import pytest
from core import db
from collectors import launch_track as track
from ops.novelties_refresh import market_offers


def test_transfer_and_raw_backfill_do_not_invent_model_dates():
    baseline=date(2026,11,6);m=dict(first_seen=date(2026,11,2),outside=0)
    assert track.creation_date(m,baseline,None)==(None,'baseline')
    m['first_seen']=date(2026,11,7)
    assert track.creation_date(m,baseline,None)==(m['first_seen'],'raw')
    assert track.creation_date(m,baseline,None,previous={'ms_created':None,'ms_source':'outside'})==(None,'outside')
    m['outside']=1
    assert track.creation_date(m,baseline,None)==(None,'outside')


def test_documented_history_and_earlier_sale():
    m=dict(first_seen=date(2026,11,2),outside=1);known=date(2026,9,4);baseline=date(2026,11,6)
    assert track.creation_date(m,baseline,None,evidence=known)==(known,'documented')
    assert track.creation_date(m,baseline,date(2026,8,1),evidence=known)==(None,'sold')
    assert track.creation_date(m,baseline,None,previous={'ms_created':known,'ms_source':'raw'})==(known,'raw')


@pytest.fixture
def tracker_schema(monkeypatch):
    original=db.get_conn;schema='novelty_tracker_test_'+uuid4().hex
    with original() as c,c.cursor() as q:
        q.execute(f'CREATE SCHEMA "{schema}"');q.execute(f'SET LOCAL search_path TO "{schema}"')
        q.execute((Path(__file__).parents[1]/'migrations/418_prc_novelties_recovery.sql').read_text())
    @contextmanager
    def isolated():
        with original() as c:
            with c.cursor() as q:q.execute(f'SET LOCAL search_path TO "{schema}"')
            yield c
    monkeypatch.setattr(db,'get_conn',isolated)
    yield
    with original() as c,c.cursor() as q:q.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_source_outage_preserves_last_observation_and_recovery_is_seeded(tracker_schema,monkeypatch):
    monkeypatch.setattr(track,'ms_models',lambda:{'0001':dict(first_seen=date(2026,11,2),outside=0,name='model',cards=1,archived=False)})
    monkeypatch.setattr(track,'novelty_meta',lambda:{})
    monkeypatch.setattr(track,'sold_before',lambda:{})
    live=lambda a:{'0001':dict(state='selling',offer='0001',note='')}
    for n in ['state_ozon','state_wb','state_ya']:monkeypatch.setattr(track,n,live)
    track.main(failed_sources={'oz_acc1'})
    assert db.query("SELECT state,card_at,selling_at FROM prc_launch_mp WHERE showcase='oz_acc1'")[0]==dict(state='unknown',card_at=None,selling_at=None)
    assert db.query('SELECT ms_created FROM prc_launch')[0]['ms_created'] is None
    track.main()
    row=db.query("SELECT state,seeded,card_at,selling_at,checked_at FROM prc_launch_mp WHERE showcase='oz_acc1'")[0]
    assert row['state']=='selling' and row['seeded'] and row['card_at'] is None and row['selling_at'] is None
    track.main(failed_sources={'oz_acc1'})
    assert db.query("SELECT state,checked_at FROM prc_launch_mp WHERE showcase='oz_acc1'")[0]==dict(state='selling',checked_at=row['checked_at'])
    assert db.query("SELECT status FROM prc_launch_source_state WHERE showcase='oz_acc1'")[0]['status']=='error'


class Response:
    status_code=200
    def __init__(self,token):self.token=token
    def raise_for_status(self):pass
    def json(self):return {'result':{'offerMappings':[{'offer':{'offerId':'0010'}}], 'paging':{'nextPageToken':self.token} if self.token else {}}}


class Session:
    def __init__(self,tokens):self.tokens=iter(tokens);self.params=[]
    def post(self,*a,**kw):self.params.append(kw['params']);return Response(next(self.tokens))


def test_market_pageToken_complete_and_repeated_cursor_guard(monkeypatch):
    from collectors import yandex_monthly as y
    monkeypatch.setenv('YANDEX_API_KEY_ACC1','test');monkeypatch.setenv('YANDEX_BUSINESS_ID_ACC1','1')
    monkeypatch.setattr(y,'_save_offers',lambda rows:len(rows))
    session=Session(['next',None]);assert market_offers(session,pause=lambda n:None)==2
    assert session.params==[{'limit':100},{'limit':100,'pageToken':'next'}]
    with pytest.raises(ValueError,match='repeated_page_token'):market_offers(Session(['same','same']),pause=lambda n:None)
