# поток: inv
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
import pytest

from core import db, ms_api
from customer_guard import ms_status, store
from tests.test_customer_guard import pg, NOW, posting, ingest


@pytest.fixture
def cache_db(pg):
    # The legacy shared fixture models a 10-minute installation; this feature
    # is verified against the currently deployed 30-minute constraint.
    db.execute('ALTER TABLE customer_guard_orders DROP CONSTRAINT customer_guard_orders_check')
    db.execute("ALTER TABLE customer_guard_orders ADD CONSTRAINT customer_guard_orders_check CHECK(ms_due_at=first_seen_at+interval '30 minutes')")
    yield


def document(uid=None, name='123-001-1', state='В пути', account=2):
    return {'id':uid or str(uuid4()),'name':name,'state':{'name':state},'updated':'2026-10-20 14:00:00',
        'organization':ms_api.ref('organization','f5bea6e0-c2ac-11ef-0a80-0221000adcc4' if account==2 else 'a5e7ee84-8d63-41f2-9fae-0be2ef90462b'),
        'agent':ms_api.ref('counterparty','b7e22828-0b7c-11ee-0a80-11480012ed47')}


def linked(cache_db, doc=None):
    doc=doc or document()
    ingest([posting()])
    row=db.query('select * from customer_guard_orders')[0]
    db.execute('update customer_guard_orders set ms_id=%s,ms_status=%s,ms_checked_at=%s where id=%s',
               (doc['id'],'Собирается',NOW-timedelta(days=5),row['id']))
    return doc,row['id']


def test_status_can_update_after_actual_cost_rechecks_stop(cache_db):
    doc,oid=linked(cache_db)
    before=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    counts={t:db.query('select count(*) n from '+t)[0]['n'] for t in ['customer_guard_checks','customer_guard_cost_sources','customer_guard_jobs']}
    assert ms_status.observe([doc],NOW)=={'observed':1,'changed':1}
    after=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert after['ms_status']=='В пути' and after['ms_status_seen_at']==NOW
    for field in before:
        if field not in ('ms_status','ms_status_seen_at'):
            assert after[field]==before[field]
    assert counts=={t:db.query('select count(*) n from '+t)[0]['n'] for t in counts}
    snapshot=db.query("select payload from customer_guard_snapshots where source='ms_status'")[0]['payload']
    assert snapshot['previous_state']=='Собирается' and snapshot['document']['state']['name']=='В пути'


def test_unchanged_status_refreshes_observation_without_duplicate_history(cache_db):
    doc,_=linked(cache_db)
    ms_status.observe([doc],NOW)
    assert ms_status.observe([doc],NOW+timedelta(minutes=30))=={'observed':1,'changed':0}
    assert db.query("select count(*) n from customer_guard_snapshots where source='ms_status'")[0]['n']==1


def test_identity_mismatch_does_not_touch_cache(cache_db):
    doc,_=linked(cache_db)
    for key in ('name','organization','agent'):
        changed=deepcopy(doc)
        changed[key]='another-order' if key=='name' else ms_api.ref(key,str(uuid4()))
        assert ms_status.observe([changed],NOW)=={'observed':0,'changed':0}
    assert db.query('select ms_status from customer_guard_orders')[0]['ms_status']=='Собирается'


def test_old_status_read_cannot_overwrite_newer_read_or_financial_check(cache_db):
    doc,oid=linked(cache_db)
    ms_status.observe([doc],NOW)
    doc['state']['name']='Собирается'
    assert ms_status.observe([doc],NOW-timedelta(minutes=1))=={'observed':0,'changed':0}
    db.execute('update customer_guard_orders set ms_checked_at=%s where id=%s',(NOW+timedelta(minutes=5),oid))
    assert ms_status.observe([doc],NOW+timedelta(minutes=1))=={'observed':0,'changed':0}


def test_final_manual_state_is_also_observed(cache_db):
    doc,_=linked(cache_db)
    doc['state']['name']='Доставлен'
    assert ms_status.observe([doc],NOW)['changed']==1
    assert db.query('select ms_status from customer_guard_orders')[0]['ms_status']=='Доставлен'


def test_slow_financial_check_cannot_restore_an_old_status(cache_db):
    doc,oid=linked(cache_db)
    at=NOW+timedelta(minutes=30)
    job=store.claim(at)
    assert job is not None
    assert ms_status.observe([doc],at+timedelta(minutes=1))['changed']==1
    old=deepcopy(doc);old['state']['name']='Собирается'
    assert store.finish(job,dict(signal='unknown'),at,old)
    saved=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert saved['ms_status']=='В пути'
    assert saved['ms_status_seen_at']==at+timedelta(minutes=1)
    assert saved['ms_checked_at']==at


def test_fresh_full_check_updates_its_own_status_timestamp(cache_db):
    doc,oid=linked(cache_db)
    at=NOW+timedelta(minutes=30)
    job=store.claim(at)
    assert store.finish(job,dict(signal='unknown'),at,doc)
    saved=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert saved['ms_status']=='В пути' and saved['ms_status_seen_at']==at


def test_older_check_cannot_relink_a_different_ms_entity(cache_db):
    doc,oid=linked(cache_db)
    at=NOW+timedelta(minutes=30)
    job=store.claim(at)
    ms_status.observe([doc],at+timedelta(minutes=1))
    new=deepcopy(doc);new['id']=str(uuid4());new['state']['name']='Подтвержден'
    assert store.finish(job,dict(signal='unknown'),at,new)
    saved=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert str(saved['ms_id'])==doc['id'] and saved['ms_status']=='В пути'
    assert saved['ms_status_seen_at']==at+timedelta(minutes=1)


def test_fresh_recreated_ms_document_does_not_inherit_old_entity_status(cache_db):
    doc,oid=linked(cache_db)
    at=NOW+timedelta(minutes=30)
    job=store.claim(at)
    ms_status.observe([doc],at+timedelta(minutes=1))
    new=deepcopy(doc);new['id']=str(uuid4());new['state']['name']='Подтвержден'
    assert store.finish(job,dict(signal='unknown'),at+timedelta(minutes=2),new)
    saved=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert str(saved['ms_id'])==new['id'] and saved['ms_status']=='Подтвержден'
    assert saved['ms_status_seen_at']==at+timedelta(minutes=2)


def test_older_missing_check_cannot_erase_known_ms_link(cache_db):
    doc,oid=linked(cache_db)
    at=NOW+timedelta(minutes=30)
    job=store.claim(at)
    ms_status.observe([doc],at+timedelta(minutes=1))
    assert store.finish(job,dict(signal='unknown'),at,None)
    saved=db.query('select * from customer_guard_orders where id=%s',(oid,))[0]
    assert str(saved['ms_id'])==doc['id'] and saved['ms_status']=='В пути'
