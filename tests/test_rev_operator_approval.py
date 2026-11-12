# поток: rev
"""Operator approval must send once without weakening unattended publication checks."""
import html
from types import SimpleNamespace
from unittest.mock import Mock,patch
from reports import publish_gate as gate
from feedback_bot import tg_moderation as bot
from tests.test_rev_review_recovery import function


def test_human_draft_is_manually_sendable_but_not_automatically():
    row=dict(kind='question',body='Подойдёт?',draft_route='human',draft_text='Здравствуйте! Да, подходит.',
             draft_grounding={'recovery_reprocessed':True,'verify':'FIX','fact_block':'проверить модель'})
    assert not gate.verdict(row)[0]
    assert gate.operator_can_send(row)
    for fields in [{'draft_text':''},{'draft_text':'⚠️ нужна проверка'},{'posted_ok':True},
                   {'is_answered':True},{'skipped_no_api':True},{'recovery_active_unanswered':False},
                   {'state':'sent'},{'state':'skipped'},{'editing':True}]:
        assert not gate.operator_can_send(dict(row,**fields))


def test_explicit_operator_approval_sends_once_and_marks_message_sent():
    raw={'platform':'wb','account':'wb_acc1','kind':'question','ext_id':'test',
         'draft_text':'Здравствуйте! Да, подходит.','draft_route':'human'}
    sets=[];edits=[];audit=[];posted=Mock(return_value=(True,'ok'))
    namespace={'_mod':lambda mid:{'state':'carded','tg_chat_id':7,'tg_msg_id':200},
               '_fr':lambda m:raw,'_set':lambda *a,**kw:sets.append((a,kw)),
               'edit_text':lambda *a,**kw:edits.append((a,kw)) or {'ok':True},
               'runtime':SimpleNamespace(publication_block=lambda:None),
               'publish_gate':gate,'fs':SimpleNamespace(post_answer=posted),
               '_remember_override':lambda *a:audit.append(a),'_cache_remember':Mock(),'html':html}
    with patch.object(gate,'verdict',return_value=(False,['неподтверждённый факт: проверить пару','маршрут human'])):
        result=function('_do_send',namespace)(1,'7',raw['draft_text'],7,200,override_block=True)
    assert result=='✅ Отправлено'
    posted.assert_called_once()
    assert 'оператор 7' in posted.call_args.kwargs['override']
    assert audit and sets[0][0]==(1,'sent')
    assert edits[0][0][2].startswith('✅ Отправлено')
    namespace['_mod']=lambda mid:{'state':'sent'}
    function('_do_send',namespace)(1,'7',raw['draft_text'],7,200,override_block=True)
    posted.assert_called_once()


def test_existing_green_callback_also_records_operator_approval():
    sent=Mock()
    namespace={'os':SimpleNamespace(getenv=lambda *a:'0'),'ALLOWED':{'7'},
               'runtime':SimpleNamespace(callback_prefix=lambda:'rev:epoch:'),
               '_mod':lambda mid:{'tg_chat_id':7,'tg_msg_id':200,'recovery_revision':'current','draft_text':'Готовый ответ'},
               '_do_send':sent,'answer_cb':Mock()}
    function('handle_callback',namespace)({'id':'cb','from':{'id':7},'data':'rev:epoch:snd:1.current',
                                         'message':{'chat':{'id':7},'message_id':200}})
    sent.assert_called_once_with(1,'7','Готовый ответ',7,200,override_block=True)


def test_keyboard_has_green_button_for_operator_override_and_epoch_revision():
    with patch.object(bot.db,'query',return_value=[{'recovery_revision':'r1'}]):
        keyboard=bot._kb(1,allow_send=False,allow_override=True)
    button=keyboard['inline_keyboard'][0][0]
    assert button['text']=='✅ Отправить'
    assert button['callback_data'].endswith('ovr:1.r1')
