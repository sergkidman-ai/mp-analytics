# поток: card
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from core import db
from tools import ozon_card_push as push, ozon_card_journal as journal
from tools import ozon_card_limit_reminder as reminder
from tools import ozon_card_daily as daily
from collectors import ozon_card_status as detector


@pytest.fixture
def sandbox(monkeypatch):
    conn=psycopg2.connect(db.DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE card_status(LIKE public.card_status INCLUDING ALL)')
        cur.execute('CREATE TEMP TABLE card_push_log(LIKE public.card_push_log INCLUDING ALL)')
        cur.execute('CREATE TEMP SEQUENCE card_test_ids')
        cur.execute("ALTER TABLE card_push_log ALTER COLUMN id SET DEFAULT nextval('pg_temp.card_test_ids')")
        sql=(Path(__file__).resolve().parents[1]/'migrations/604_card_recovery_journal.sql').read_text()
        sql=sql.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE IF NOT EXISTS')
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
        with connection() as con, con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql,params)
            return [dict(row) for row in cur.fetchall()]
    def execute(sql,params=None):
        with connection() as con,con.cursor() as cur:
            cur.execute(sql,params);return cur.rowcount
    monkeypatch.setattr(db,'get_conn',connection)
    monkeypatch.setattr(db,'query',query)
    monkeypatch.setattr(db,'execute',execute)
    monkeypatch.setattr(push,'query',query)
    monkeypatch.setattr(push,'execute',execute)
    yield query,execute
    conn.close()


def insert(execute,offer='new',missing=False,product=7):
    execute("""INSERT INTO card_status(platform,account,offer_id,product_id,err_class,
        status_failed,is_open,history_missing) VALUES('ozon','oz_acc2',%s,%s,'A','imported',true,%s)""",
        (offer,product,missing))


def test_lost_history_is_held_and_daily_recovery_budget_cannot_be_bypassed(sandbox):
    query,execute=sandbox
    for i in range(25):insert(execute,f'old{i:02}',True,100+i)
    insert(execute)
    assert [r['offer_id'] for r in journal.pool('oz_acc2',500,['A'])]==['new']
    preview=journal.pool('oz_acc2',500,['A'],preview=True)
    old=[r for r in preview if r['history_missing']]
    assert len(old)==20
    assert journal.reserve('oz_acc2',old[0],{}) is None
    execute('UPDATE card_recovery_control SET legacy_once_enabled=true')
    for card in old:
        assert journal.reserve('oz_acc2',card,{}) is not None
        assert journal.reserve('oz_acc2',card,{}) is None
    extra=query("SELECT * FROM card_status WHERE offer_id='old24'")[0]
    assert journal.reserve('oz_acc2',extra,{}) is None
    assert query('SELECT count(*) n FROM card_push_log')[0]['n']==20
    assert [r['offer_id'] for r in journal.pool('oz_acc2',500,['A'])]==['new']


def test_known_history_keeps_three_attempt_limit_and_twenty_hour_backoff(sandbox):
    query,execute=sandbox;insert(execute)
    for attempt in range(3):
        card=query("SELECT * FROM card_status WHERE offer_id='new'")[0]
        identity=journal.reserve('oz_acc2',card,{})
        assert identity is not None
        assert journal.reserve('oz_acc2',card,{}) is None
        execute("UPDATE card_push_log SET task_status='imported',healed=false WHERE id=%s",(identity,))
        assert journal.reserve('oz_acc2',card,{}) is None
        execute("UPDATE card_status SET last_attempt_at=now()-interval '21 hours'")
    assert journal.reserve('oz_acc2',card,{}) is None
    assert query('SELECT attempts FROM card_status')[0]['attempts']==3


def test_uncertain_send_is_durable_before_request_and_never_automatically_repeated(sandbox,monkeypatch):
    query,execute=sandbox;insert(execute)
    monkeypatch.setattr(push,'_headers',lambda account:{})
    monkeypatch.setattr(push,'_live_states',lambda *args:{'new':dict(status_failed='imported',_codes=set(),_hard_errors=[],status_updated_at='2026-01-01T00:00:00Z')})
    def send(headers,offer,identity,product,expected):
        row=query('SELECT * FROM card_push_log WHERE id=%s',(identity,))[0]
        assert row['task_status']=='prepared'
        assert query('SELECT attempts FROM card_status')[0]['attempts']==1
        raise TimeoutError('unknown response')
    monkeypatch.setattr(push,'_send',send)
    with pytest.raises(TimeoutError):push.run('oz_acc2',20,True)
    assert query('SELECT task_status FROM card_push_log')[0]['task_status']=='uncertain'
    assert journal.pool('oz_acc2',20,['A'])==[]


def test_wrong_offer_identity_never_reaches_update_endpoint(monkeypatch):
    response=Mock();response.json.return_value={'result':[dict(id=7,offer_id='other',attributes=[])]}
    post=Mock(return_value=response);monkeypatch.setattr(push.requests,'post',post)
    with pytest.raises(ValueError,match='identity'):push._send({},'wanted',1,7)
    assert post.call_count==1 and post.call_args.args[0]==push.ATTR_URL


def test_noop_payload_is_original_value_and_is_recorded_before_update(monkeypatch):
    captured=[];recorded=[]
    def post(url,**kwargs):
        captured.append((url,kwargs['json']))
        response=Mock();response.status_code=200
        if url==push.ATTR_URL:
            response.json.return_value={'result':[dict(id=7,offer_id='wanted',attributes=[dict(id=9024,complex_id=0,values=[{'value':'0001-original'}])])]}
        else:
            assert recorded and recorded[0]==kwargs['json']
            response.json.return_value={'task_id':123}
        return response
    monkeypatch.setattr(push.requests,'post',post)
    monkeypatch.setattr(push,'_live_states',lambda *args:{'wanted':dict(status_updated_at='2026-01-01T00:00:00Z',_hard_errors=[])})
    monkeypatch.setattr(journal,'save_payload',lambda identity,body:recorded.append(body))
    assert push._send({},'wanted',1,7)[2]==123
    assert captured[-1][1]['items'][0]['attributes'][0]['values']==[{'value':'0001-original'}]


@pytest.mark.parametrize('task_status,expected',[('imported',1),('skipped',0),('failed',0),('pending',0)])
def test_warning_or_flag_clearance_requires_imported_receipt(monkeypatch,task_status,expected):
    monkeypatch.setattr(push,'_live_states',lambda *args:{'offer':dict(_codes=set(),_hard_errors=[],moderate_status='approved',status_name='Продается')})
    monkeypatch.setattr(push,'_task_status',lambda *args,**kwargs:task_status)
    monkeypatch.setattr(push,'execute',Mock())
    healed=Mock();monkeypatch.setattr(push,'_mark_healed',healed)
    sent=[dict(id=1,offer_id='offer',product_id=7,task_id=123,task_status=task_status)]
    assert push._verify({},'oz_acc2',sent,{'offer':('A',set())})[0]==expected
    assert healed.call_count==expected


def test_pending_receipt_keeps_verification_open(sandbox,monkeypatch):
    query,execute=sandbox;insert(execute)
    card=query('SELECT * FROM card_status')[0]
    identity=journal.reserve('oz_acc2',card,{'class':'A'})
    execute("UPDATE card_push_log SET task_id=123,task_status='pending' WHERE id=%s",(identity,))
    monkeypatch.setattr(push,'_live_states',lambda *args:{'new':dict(_codes=set(),_hard_errors=[],status_name='Продается')})
    monkeypatch.setattr(push,'_task_status',lambda *args,**kwargs:'pending')
    rows=query('SELECT * FROM card_push_log')
    assert push._verify({},'oz_acc2',rows,{'new':('A',set())})==(0,None)
    assert query('SELECT healed,action_state FROM card_push_log')[0]==dict(healed=None,action_state='pending')
    assert not daily.settle('oz_acc2')
    assert journal.pool('oz_acc2',20,['A'])==[]


def test_accepted_before_crash_is_verified_without_resubmission(sandbox,monkeypatch):
    query,execute=sandbox;insert(execute)
    identity=journal.reserve('oz_acc2',query('SELECT * FROM card_status')[0],{'class':'A'})
    execute("UPDATE card_push_log SET task_id=123,task_status='imported' WHERE id=%s",(identity,))
    monkeypatch.setattr(push,'_live_states',lambda *args:{'new':dict(_codes=set(),_hard_errors=[],status_name='Продается')})
    post=Mock(side_effect=AssertionError('unexpected update'))
    monkeypatch.setattr(push.requests,'post',post)
    assert daily.settle('oz_acc2')
    assert query('SELECT healed FROM card_push_log')[0]['healed'] is True
    assert query('SELECT attempts,is_open FROM card_status')[0]==dict(attempts=1,is_open=False)
    post.assert_not_called()


def test_failed_gate_survives_crash_before_verification(sandbox,monkeypatch):
    query,execute=sandbox
    for i in range(20):
        insert(execute,f'card{i}',product=100+i)
    for card in query('SELECT * FROM card_status'):
        identity=journal.reserve('oz_acc2',card,{'class':'A','batch_key':'recovered-batch'})
        execute("UPDATE card_push_log SET task_id=123,task_status='imported' WHERE id=%s",(identity,))
    monkeypatch.setattr(push,'_live_states',lambda *args:{f'card{i}':dict(status_failed='imported',_codes=set(),_hard_errors=[],status_name='Продается') for i in range(20)})
    assert not daily.settle('oz_acc2')
    assert journal.control()['paused_reason']=='gate_failed:oz_acc2'
    assert query('SELECT count(*) n FROM card_push_log WHERE healed IS NULL')[0]['n']==0


def test_detector_rejects_incomplete_information_instead_of_closing_missing_cards(monkeypatch):
    monkeypatch.setattr(detector,'_headers',lambda account:{})
    monkeypatch.setattr(detector,'control',lambda:{'restored_at':datetime(2026,1,1,tzinfo=timezone.utc)})
    monkeypatch.setattr(detector,'_ids',lambda headers,visibility:[7,8])
    response=Mock();response.json.return_value={'items':[dict(id=7,offer_id='one',statuses={'status':'ok'})]}
    monkeypatch.setattr(detector.requests,'post',lambda *args,**kwargs:response)
    with pytest.raises(ValueError,match='Incomplete'):detector.scan('oz_acc2')


def test_old_naive_time_is_supported_and_unknown_freshness_is_held():
    assert push._too_fresh({'status_updated_at':'2026-01-01T00:00:00'}) is False
    assert push._too_fresh({'status_updated_at':'invalid'}) is True


def test_historical_oneoff_notification_is_not_replayed(monkeypatch,tmp_path):
    send=Mock();monkeypatch.setattr(reminder,'send_text',send)
    assert reminder.send_due(now=reminder.DUE+timedelta(days=30),marker=tmp_path/'marker') is False
    send.assert_not_called()


def test_cleared_card_is_not_reposted_after_last_moment_check(monkeypatch):
    response=Mock();response.json.return_value={'result':[dict(id=7,offer_id='wanted',attributes=[dict(id=9024,values=[{'value':'original'}])])]}
    post=Mock(return_value=response);monkeypatch.setattr(push.requests,'post',post)
    monkeypatch.setattr(push,'_live_states',lambda *args:{'wanted':dict(status_updated_at='2026-01-01T00:00:00Z',_hard_errors=[],_codes=set())})
    assert push._send({},'wanted',1,7,('A',set()))[1] is None
    assert post.call_count==1
