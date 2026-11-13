# поток: rev
"""New question cards arrive immediately, without buyer publication or duplicate cards."""
from contextlib import nullcontext
from unittest.mock import patch,Mock
from ops import rev_question_fast_poll as fast


def test_new_question_receives_one_manual_card_and_existing_card_is_skipped():
    fresh={'id':9,'state':'queued','tg_msg_id':None,'decided_at':None,
           'draft_text':'','kind':'question','platform':'ozon','account':'oz_acc1'}
    shown=dict(fresh,id=10,tg_msg_id=123)
    with patch.object(fast.collect,'report',[]), \
         patch.object(fast.collect,'stage',side_effect=lambda *a:None), \
         patch.object(fast.db,'execute'), \
         patch.object(fast.db,'query',return_value=[{'id':9},{'id':10}]), \
         patch.object(fast.runtime,'card_lock',side_effect=lambda *a:nullcontext()), \
         patch.object(fast.review,'load',side_effect=[fresh,shown]), \
         patch.object(fast.publish_gate,'verdict_full',return_value=(False,['no draft'],[])), \
         patch.object(fast.publish_gate,'operator_can_send',return_value=False), \
         patch.object(fast.bot,'_card',return_value='Question'), \
         patch.object(fast.bot,'_kb',return_value={'inline_keyboard':[]}), \
         patch.object(fast.bot,'send',return_value=456) as send, \
         patch.object(fast.bot,'_set') as save:
        assert fast.run()['new_question_cards']==1
    send.assert_called_once()
    save.assert_called_once()
    assert save.call_args.args[:2]==(9,'carded')
