# поток: inv
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from ops import fbs_ozon_failure_status as f
from shipment_guard.config import ORGS, AGENTS

UID='124cab0d-53b5-4ddb-bae1-c59d9373fada'


def order(account=1,state='Собирается'):
    def ref(kind,uid):return {'meta':{'href':'https://example/entity/'+kind+'/'+uid}}
    return {'id':UID,'name':'123456-0010-1','state':dict(ref('state','old'),name=state),
            'organization':ref('organization',list(ORGS)[account-1]),
            'agent':ref('counterparty',next(k for k,v in AGENTS.items() if v=='ozon'))}


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(f,'account_lock',lambda *a,**k:nullcontext(Mock()))
    session=Mock();session._ozon_get.return_value={'status':'awaiting_packaging'}
    original=order();after=deepcopy(original)
    after['state']={'name':f.TARGET_NAME,'meta':{'href':'https://example/states/'+f.TARGET_ID}}
    result={'platform':'ozon','account':'oz_acc1','number':original['name'],'order_id':UID,
            'result':'blocked','reason':'real_gtd_missing_or_ambiguous_user_selection_required'}
    get=Mock(side_effect=[original,after]);request=Mock(return_value=Mock(status_code=200))
    return tmp_path,session,original,after,result,get,request


def call(s):
    folder,session,original,after,result,get,request=s
    return f.sync_failure_status(original,folder,result,session,get=get,request=request)


def test_gtd_failure_writes_only_ms_status_with_readback(setup):
    result=call(setup);request=setup[-1]
    assert result['ms_status_feedback']['result']=='updated'
    assert request.call_count==1 and request.call_args.args[0]=='PUT'
    payload=request.call_args.kwargs['json']
    assert set(payload)=={'state'} and payload['state']['meta']['href'].endswith('/'+f.TARGET_ID)
    assert (setup[0]/'ms_ozon_failure_intent.json').exists()


@pytest.mark.parametrize('state',['awaiting_deliver','delivering','delivered','cancelled'])
def test_no_failure_flag_for_advanced_or_cancelled_postings(setup,state):
    setup[1]._ozon_get.return_value={'status':state}
    result=call(setup)
    setup[-1].assert_not_called();setup[-2].assert_not_called()
    if state!='cancelled':assert result['result']=='already_ready'


def test_posting_read_timeout_is_not_a_confirmed_failure(setup):
    setup[1]._ozon_get.side_effect=requests.ReadTimeout()
    assert call(setup)['ms_status_feedback']['result']=='error'
    setup[-1].assert_not_called();setup[-2].assert_not_called()


def test_does_not_overwrite_a_manually_changed_or_completed_order(setup):
    setup[2]['state']['name']='Доставлен'
    assert call(setup)['ms_status_feedback']['reason']=='ms_order_state_changed'
    setup[-1].assert_not_called()


def test_ms_account_identity_is_checked_before_write(setup):
    foreign=deepcopy(setup[2]);foreign['organization']=order(2)['organization']
    setup[-2].side_effect=[foreign]
    assert call(setup)['ms_status_feedback']['reason'].endswith('ms_order_identity_changed')
    setup[-1].assert_not_called()


def test_status_write_timeout_is_reconciled_without_repeating_put(setup):
    setup[-1].side_effect=requests.ReadTimeout()
    assert call(setup)['ms_status_feedback']['result']=='updated'
    assert setup[-1].call_count==1


def test_ms_success_response_alone_is_not_state_confirmation(setup):
    setup[-2].side_effect=[setup[2],deepcopy(setup[2])]
    result=call(setup)
    assert result['ms_status_feedback']['result']=='uncertain'
    assert result['ms_status_feedback']['verified'] is False
    assert setup[-1].call_count==1


def test_dsquare_uses_the_same_status_only_rule(setup):
    current=order(2);after=deepcopy(current);after['state']=setup[3]['state']
    setup[2]['organization']=current['organization'];setup[4]['account']='oz_acc2'
    setup[-2].side_effect=[current,after]
    assert call(setup)['ms_status_feedback']['result']=='updated'
    assert setup[-1].call_args.kwargs['json']['state']['meta']['href'].endswith('/'+f.TARGET_ID)


def test_previous_intent_without_confirmation_never_repeats_put(setup):
    (setup[0]/'ms_ozon_failure_intent.json').write_text('{}')
    assert call(setup)['ms_status_feedback']['result']=='uncertain'
    setup[-1].assert_not_called()


def test_already_marked_order_is_idempotent(setup):
    setup[-2].side_effect=None;setup[-2].return_value=setup[3]
    assert call(setup)['ms_status_feedback']['result']=='already_marked'
    setup[-1].assert_not_called()


def test_successful_result_needs_no_failure_feedback(setup):
    setup[4]['result']='ready'
    assert call(setup)==setup[4]
    setup[1]._ozon_get.assert_not_called();setup[-1].assert_not_called()


def test_old_success_ack_is_rechecked_and_marked_if_posting_did_not_advance(setup):
    setup[4].update(result='ready',reason='ozon_ship_api_confirmed')
    result=call(setup)
    assert result['result']=='blocked' and result['ms_status_feedback']['result']=='updated'
    assert setup[-1].call_count==1


def test_both_public_market_entrypoints_attach_feedback(monkeypatch,tmp_path):
    from ops import fbs_market_fulfillment as market
    o=order();session=Mock();session.fulfill.return_value={'result':'blocked'}
    session.fulfill_many.return_value={o['id']:{'result':'blocked'}}
    monkeypatch.setattr(market,'_DEFAULT',session)
    sync=Mock(return_value={'result':'blocked','ms_status_feedback':{'result':'updated'}})
    monkeypatch.setattr(f,'sync_failure_status',sync)
    assert market.fulfill(o,tmp_path)['ms_status_feedback']['result']=='updated'
    assert market.fulfill_many([o],tmp_path)[o['id']]['ms_status_feedback']['result']=='updated'
    assert sync.call_count==2
