# поток: prc
"""Full catalogue recovery must include archived and imported (non-time-ID) cards."""
import json
from uuid import uuid4
from core import db, ms_api
from tools.prc import restore_launch_catalogue_dates as tool


def test_all_models_include_archived_non_time_ids_and_keep_missing_create_unknown(tmp_path, monkeypatch):
    timed='f784342c-a84f-11f1-0a80-182d00617a6a'
    imported=str(uuid4())
    old='5800ae21-6bc3-11e4-90a2-8ecb001f442c'
    cards=[dict(id=timed,externalCode='7000',code='7000at',archived=False,name='New supplier'),
           dict(id=imported,externalCode='7000',code='7000bt',archived=True,name='Archived supplier'),
           dict(id=old,externalCode='8000',code='8000at',archived=True,name='No CREATE history')]
    calls=[]
    def fetch(url, params):
        calls.append((url,params))
        if url=='/entity/product':
            assert params['filter']=='archived=true;archived=false'
            return {'meta':{'size':3},'rows':cards}
        pid=url.split('/')[-2]
        kind,moment=('update','2015-12-09 10:53:13.314') if pid==old else ('create','2020-01-02 15:00:00.000' if pid==imported else '2026-09-04 14:01:23.427')
        return {'meta':{'size':1},'rows':[dict(eventType=kind,moment=moment,entity={'meta':{'href':'https://api/entity/product/'+pid}})]}
    monkeypatch.setattr(ms_api,'get',fetch)
    monkeypatch.setattr(db,'query',lambda *a:[{'external_code':'7000'},{'external_code':'8000'}])
    monkeypatch.setattr('sys.argv',['restore','--output',str(tmp_path),'--workers','2'])
    tool.main()
    records=json.loads((tmp_path/'records.json').read_text())
    assert len(records)==1 and records[0]['external_code']=='7000'
    assert records[0]['ms_created']=='2020-01-02' and records[0]['ms_id']==imported
    assert json.loads((tmp_path/'unresolved.json').read_text())[0]['external_code']=='8000'
    assert json.loads((tmp_path/'complete.json').read_text())['missing_models']==[]
    assert len(calls)==4
    monkeypatch.setattr(ms_api,'get',lambda *a:(_ for _ in ()).throw(AssertionError('unexpected repeated API call')))
    tool.main()
    assert json.loads((tmp_path/'records.json').read_text())==records
