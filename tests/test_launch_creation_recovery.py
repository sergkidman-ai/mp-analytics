# поток: prc
from datetime import date
from contextlib import contextmanager
from uuid import uuid4
import pytest
from core import db
from tools.prc.restore_launch_dates import creation_event, oldest_card, uuid_time, apply_evidence

CARD='f784342c-a84f-11f1-0a80-182d00617a6a'

def event(card=CARD,kind='create',moment='2026-09-04 14:01:23.427'):
    return dict(eventType=kind,moment=moment,entity={'meta':{'href':'https://api/entity/product/'+card}})


def test_audit_tail_recovery_uses_create_in_ms_timezone_not_last_update():
    calls=[]
    def get(path,params):
        calls.append(params)
        if params.get('offset')==80:return {'meta':{'size':100},'rows':[event()]}
        return {'meta':{'size':100},'rows':[event(kind='update',moment='2026-11-08 01:00:00.000')]}
    day,e,pages=creation_event({'ms_id':CARD},get)
    assert day==date(2026,9,4) and len(pages)==2 and calls[1]['offset']==80
    assert uuid_time(CARD).hour==11  # MS's time-based IDs have non-RFC variant bits.


@pytest.mark.parametrize('row',[event(kind='update'),event(card='other'),event(moment='2026-10-04 14:01:23.427')])
def test_missing_or_wrong_create_never_produces_a_model_date(row):
    with pytest.raises(ValueError):creation_event({'ms_id':CARD},lambda *a:{'meta':{'size':1},'rows':[row]})


def test_archived_older_card_wins_over_new_supplier_card():
    newer='0f6d7c8c-db70-11f1-0a80-1bb6009f3613'
    assert oldest_card([{'ms_id':newer,'archived':False},{'ms_id':CARD,'archived':True}])['ms_id']==CARD
    with pytest.raises(ValueError):oldest_card([{'ms_id':CARD},{'ms_id':str(uuid4())}])


def test_local_evidence_is_idempotent_preserves_known_dates_and_rejects_earlier_sales(monkeypatch):
    original=db.get_conn;schema='launch_create_test_'+uuid4().hex
    with original() as c,c.cursor() as q:
        q.execute(f'CREATE SCHEMA "{schema}"');q.execute(f'SET LOCAL search_path TO "{schema}"')
        q.execute('''CREATE TABLE prc_launch(external_code text PRIMARY KEY,ms_created date,ms_source text);
        CREATE TABLE prc_launch_creation_evidence(external_code text PRIMARY KEY,ms_created date,source_ref text);
        CREATE TABLE ms_demand_pos(ms_id text,demand_id text);
        CREATE TABLE ms_demand_cogs(demand_id text,moment timestamp);
        CREATE TABLE ms_product(ms_id text,external_code text);
        INSERT INTO prc_launch VALUES('7000',NULL,'baseline'),('7001',NULL,'baseline'),('7002','2026-01-01','documented');
        INSERT INTO ms_product VALUES('old','7001');
        INSERT INTO ms_demand_pos VALUES('old','sale');
        INSERT INTO ms_demand_cogs VALUES('sale','2026-08-01');''')
    @contextmanager
    def isolated():
        with original() as c:
            with c.cursor() as q:q.execute(f'SET LOCAL search_path TO "{schema}"')
            yield c
    monkeypatch.setattr(db,'get_conn',isolated)
    records=[dict(external_code=k,ms_created='2026-09-04',source_ref='audit-proof') for k in ['7000','7001','7002']]
    try:
        assert apply_evidence(records)==dict(restored=1,already_known=1,earlier_sale=1)
        assert apply_evidence(records)['restored']==0
        assert db.query("SELECT ms_created FROM prc_launch WHERE external_code='7002'")[0]['ms_created']==date(2026,1,1)
        assert db.query('SELECT count(*) n FROM prc_launch_creation_evidence')[0]['n']==1
        earlier=[dict(external_code='7000',ms_created='2025-01-02',source_ref='archived-card-create')]
        assert apply_evidence(earlier)['already_known']==1
        corrected=apply_evidence(earlier,allow_earlier=True)
        assert corrected['corrected']==1 and corrected['restored']==0
        assert apply_evidence(earlier,allow_earlier=True)['corrected']==0
        assert db.query("SELECT ms_created FROM prc_launch WHERE external_code='7000'")[0]['ms_created']==date(2025,1,2)
    finally:
        with original() as c,c.cursor() as q:q.execute(f'DROP SCHEMA "{schema}" CASCADE')
