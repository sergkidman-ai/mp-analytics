# поток: inv
from copy import deepcopy
import json
from pathlib import Path
import pytest
from core import order_composition_guard as guard

OID='7b198bd3-d9b5-11f1-0a80-16db001e45f8'
DATE='2026-11-09 12:35:18.962'

def position(pid='old',quantity=1,reserve=0):
    return dict(id='row',assortment={'meta':{'href':'https://api/entity/product/'+pid,'type':'product'}},
                quantity=quantity,price=10000,discount=0,vat=5,vatEnabled=True,reserve=reserve)


def order():
    return dict(id=OID,name='6207815414',updated=DATE,positions={'meta':{'size':1},'rows':[position()]})


def event(source='app',diff=None,oid=OID,moment=DATE):
    return dict(source=source,eventType='update',moment=moment,uid='operator',
                entity={'meta':{'href':'https://api/entity/customerorder/'+oid}},
                diff=diff or {},audit={'meta':{'href':'https://api/audit/context'}})


def history_read(events,current=None):
    calls=[]
    current=current or order()
    def read(path,params=None):
        calls.append((path,params))
        if path.endswith('/audit'):
            offset=params.get('offset',0)
            return dict(meta={'size':len(events)},rows=events[offset:offset+20])
        if '/audit/' in path:return dict(meta={'size':len(events)},rows=events)
        return deepcopy(current)
    return read,calls


@pytest.fixture(autouse=True)
def flags(tmp_path,monkeypatch):
    monkeypatch.setattr(guard,'PROTECTION_DIR',tmp_path/'manual_locks')


def test_manual_swap_remains_protected_after_later_automatic_replacement():
    diff={'positions':[{'oldValue':position('blossom')},{'newValue':position('odissey')}]}
    read,calls=history_read([event('remap-1.2'),event('app',diff)])
    with pytest.raises(guard.ManualCompositionProtected):guard.assert_automatic_composition(order(),read)
    proof=json.loads((guard.PROTECTION_DIR/(OID+'.json')).read_text())
    assert proof['source']=='app' and proof['reason']=='manual_user_composition'
    with pytest.raises(guard.ManualCompositionProtected):guard.assert_automatic_composition(order(),lambda *a:pytest.fail('sticky lock must survive without network'))


def test_comment_status_or_reserve_only_manual_edit_does_not_lock():
    events=[event(diff={'description':{'newValue':'comment'}}),
            event(diff={'positions':[dict(oldValue=position(reserve=0),newValue=position(reserve=1))]}),
            event('remap-1.2',{'positions':[dict(oldValue=position(),newValue=position('other'))]})]
    read,_=history_read(events)
    guard.assert_automatic_composition(order(),read)
    assert not guard.PROTECTION_DIR.exists()


@pytest.mark.parametrize('field,value',[('quantity',2),('price',20000),('discount',10),('vat',7)])
def test_manual_sale_line_changes_protect_order(field,value):
    before=position();after=dict(before,**{field:value})
    read,_=history_read([event(diff={'positions':[dict(oldValue=before,newValue=after)]})])
    with pytest.raises(guard.ManualCompositionProtected):guard.assert_automatic_composition(order(),read)


def test_full_history_not_only_latest_page():
    rows=[event('remap-1.2') for _ in range(22)]+[event(diff={'positions':[dict(newValue=position())]})]
    read,calls=history_read(rows)
    with pytest.raises(guard.ManualCompositionProtected):guard.assert_automatic_composition(order(),read)
    assert any(params['offset']==20 for _,params in calls)


def test_shared_audit_context_is_filtered_to_exact_document():
    summary=event();summary.pop('diff')
    foreign=event(diff={'positions':[dict(newValue=position())]},oid='foreign')
    def read(path,params):
        return dict(meta={'size':1},rows=[summary]) if path.endswith('/audit') else dict(meta={'size':2},rows=[foreign,event(diff={'description':{}})])
    guard.assert_automatic_composition(order(),read)


@pytest.mark.parametrize('mode',['network','incomplete','stale','unknown_actor'])
def test_unverified_history_never_authorizes_change(mode):
    read,_=history_read([event('remap-1.2')])
    if mode=='network':
        def read(*a):raise TimeoutError()
    elif mode=='incomplete':
        def read(*a):return dict(meta={'size':2},rows=[])
    elif mode=='stale':read,_=history_read([event('remap-1.2',moment='2026-11-08 00:00:00')])
    elif mode=='unknown_actor':read,_=history_read([event('unverified-import',{'positions':[dict(newValue=position())]})])
    with pytest.raises(guard.ManualCompositionUnverified):guard.assert_automatic_composition(order(),read)


def test_transport_guard_blocks_composition_but_allows_same_lines_reservation_update():
    read,calls=history_read([event(diff={'positions':[dict(newValue=position())]})])
    with pytest.raises(guard.ManualCompositionProtected):
        guard.assert_write_allowed('PUT','/entity/customerorder/'+OID,{'positions':[position('replacement')]},read)
    guard.assert_write_allowed('PUT','/entity/customerorder/'+OID,{'positions':[position(reserve=1)],'state':{}},read)
    guard.assert_write_allowed('PUT','/entity/customerorder/'+OID,{'positions':[{'id':'row','reserve':1}]},read)
    before=len(calls)
    guard.assert_write_allowed('PUT','/entity/customerorder/'+OID,{'state':{}},read)
    guard.assert_write_allowed('POST','/entity/customerorder',{'positions':[position()]},read)
    assert len(calls)==before


def test_batch_and_position_subresource_cannot_bypass_guard():
    read,_=history_read([event(diff={'positions':[dict(newValue=position())]})])
    for method,path,payload in [('POST','/entity/customerorder',[dict(id=OID,positions=[position('new')])]),
            ('POST','/entity/customerorder/'+OID+'/positions',[position('new')]),
            ('DELETE','/entity/customerorder/'+OID+'/positions/row',None)]:
        with pytest.raises(guard.ManualCompositionProtected):guard.assert_write_allowed(method,path,payload,read)


def test_change_during_check_blocks_clean_order_write():
    read,_=history_read([event('remap-1.2')]);old=read
    def changed(path,params=None):
        result=old(path,params)
        if path.endswith(OID) and params is None:result['updated']='2026-11-09 13:00:00'
        return result
    with pytest.raises(guard.ManualCompositionUnverified,match='changed_before'):
        guard.assert_write_allowed('PUT','/entity/customerorder/'+OID,{'positions':[position('new')]},changed)


def test_actual_http_write_is_never_attempted_for_protected_order(monkeypatch):
    from core import ms_api
    read,_=history_read([event(diff={'positions':[dict(newValue=position())]})])
    monkeypatch.setattr(ms_api,'get',read)
    monkeypatch.setattr(ms_api.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('must not attempt write'))
    with pytest.raises(guard.ManualCompositionProtected):ms_api.put('/entity/customerorder/'+OID,{'positions':[position('new')]})


def test_client_assigned_id_for_new_order_is_not_mistaken_for_existing_edit():
    from core.ms_api import MsError
    def missing(*args):raise MsError(404,{})
    guard.assert_write_allowed('POST','/entity/customerorder',{'id':OID,'positions':[position()]},missing)
