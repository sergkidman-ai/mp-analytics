# поток: rev
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reports.recovery_review import eligible,unsupported_origins,owner_actions
from reports import llm_batch
from reports import brand_notes
from reports.feedback_llm import _web_block
from reports.feedback_today import _fix_producer, _is_compat_q
from reports import publish_gate
from reports import deepseek_search

ROOT = Path(__file__).resolve().parents[1]


def function(name, namespace):
    tree = ast.parse((ROOT/'feedback_bot/tg_moderation.py').read_text())
    node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT),'exec'),namespace)
    return namespace[name]


class ReviewRecovery(unittest.TestCase):
    def test_photo_request_is_owner_action_even_if_compatibility_is_known(self):
        self.assertTrue(owner_actions('Подойдёт для Pro9431? Можно фото в упаковке?'))
        self.assertFalse(owner_actions('Подойдёт для Pro9431?'))

    def test_system_prompt_change_invalidates_old_generation_cache(self):
        row={'platform':'wb','account':'a','kind':'question','ext_id':'1'}
        first=llm_batch._key('model','draft',row,'same content','old system')
        self.assertNotEqual(first,llm_batch._key('model','draft',row,'same content','new system'))
        self.assertEqual(first,llm_batch._key('model','draft',row,'same content','old system'))

    def test_inflected_country_claim_requires_evidence_for_this_lot(self):
        self.assertEqual(unsupported_origins('Произведён в Китае','чип установлен'),['китае'])
        self.assertEqual(unsupported_origins('Производство Китай',''),['китай'])

    def test_raw_search_cache_is_not_treated_as_validated_primary_source(self):
        with patch.object(deepseek_search.db,'query',side_effect=[[{'name':'rev_search_cache'}],[{'evidence':{'answer':'unverified'}}]]):
            self.assertIsNone(deepseek_search.cached_primary('question','product'))

    def test_validated_scoped_evidence_is_reused_without_new_paid_search(self):
        evidence={'validated_primary':True,'answer':'fact','scope':{'printer':'exact'}}
        with patch.object(deepseek_search.db,'query',side_effect=[[{'name':'rev_search_cache'}],[{'evidence':evidence}]]):
            self.assertEqual(deepseek_search.cached_primary('question','product'),evidence)

    def test_country_is_not_invented_for_producer_question(self):
        answer='Здравствуйте! Совместимый аналог.'
        self.assertEqual(_fix_producer(answer,'Кто производитель?'),answer)

    def test_selection_without_li_uses_model_compatibility_path(self):
        self.assertTrue(_is_compat_q('Есть картриджи на c460 fw?'))

    def test_unconfirmed_recovery_fact_cannot_be_overridden_with_send_as_is(self):
        row={'kind':'question','draft_route':'review','draft_text':'В наличии',
             'draft_grounding':{'fact_block':'нет подтверждения наличия','verify':'PASS'}}
        allowed,reasons,_=publish_gate.verdict_full(row)
        self.assertFalse(allowed)
        self.assertFalse(publish_gate.can_override(reasons))

    def test_search_sources_accept_structured_results_and_keep_relevant_evidence(self):
        block=_web_block({'sources':[{'url':'https://example.org/spec','title':'Specification'}],
                          'answer':'a'*1000+' VERIFIED'})
        self.assertIn('https://example.org/spec',block)
        self.assertIn('VERIFIED',block)

    def test_operator_decisions_and_edit_sessions_prevent_replacement(self):
        original = {'state':'carded'}
        self.assertTrue(eligible(original))
        for change in [{'final_text':'manual'}, {'decided_at':'now'}, {'editing':True},
                       {'state':'edited'}, {'state':'skipped'}, {'is_answered':True},
                       {'posted_ok':True}, {'recovery_active_unanswered':False}]:
            with self.subTest(change=change):
                self.assertFalse(eligible(dict(original,**change)))

    def test_old_version_cannot_send_updated_text_from_same_message(self):
        replies=[]
        namespace={'os':SimpleNamespace(getenv=lambda *a:'0'),'ALLOWED':{'7'},
          'runtime':SimpleNamespace(callback_prefix=lambda:'rev:epoch:'),
          '_mod':lambda mid:{'tg_chat_id':7,'tg_msg_id':200,'recovery_revision':'new'},
          'answer_cb':lambda *args:replies.append(args)}
        callback=function('handle_callback',namespace)
        for suffix in ['1','1.old']:
            callback({'id':'cb','from':{'id':7},'data':'rev:epoch:snd:'+suffix,
                      'message':{'chat':{'id':7},'message_id':200}})
        self.assertEqual(len(replies),2)
        self.assertTrue(all('Ответ обновлён' in r[1] for r in replies))

    def test_manual_edit_is_saved_then_sent_without_second_confirmation(self):
        saved=[];edits=[];finished=[];sent=[]
        namespace={'ALLOWED':{'7'},'PENDING_EDIT':SimpleNamespace(pop=lambda *a:1,finish=finished.append),
          'clean_operator_text':lambda text:(text,None),
          '_mod':lambda mid:{'state':'carded','tg_chat_id':7,'tg_msg_id':200},
          '_set':lambda *args,**kwargs:saved.append((args,kwargs)),
          '_do_send':lambda *args,**kwargs:sent.append((args,kwargs)),
          'edit_text':lambda *a,**k:edits.append((a,k)),
          '_kb':lambda *a,**k:{'inline_keyboard':[]},
          'html':__import__('html')}
        handler=function('handle_message',namespace)
        handler({'from':{'id':7},'chat':{'id':7},'text':'Ручной ответ'})
        self.assertEqual(saved[0][0],(1,'edited'))
        self.assertEqual(saved[0][1]['final_text'],'Ручной ответ')
        self.assertEqual(edits,[])
        self.assertEqual(sent,[((1,'7','Ручной ответ',7,None),{'operator_edit':True})])
        self.assertEqual(finished,['7'])

    def test_edit_commands_never_become_customer_answers(self):
        for command in ('/next','/all','/note_add hp | series | text','/cancel','/cancel@reviewswbozon2_bot'):
            with self.subTest(command=command):
                sent=[];finished=[];notices=[]
                namespace={'ALLOWED':{'7'},'PENDING_EDIT':SimpleNamespace(pop=lambda *a:1,finish=finished.append),
                           'send':lambda *args:notices.append(args),
                           '_do_send':lambda *args,**kwargs:sent.append(args)}
                function('handle_message',namespace)({'from':{'id':7},'chat':{'id':7},'text':command})
                self.assertEqual(sent,[])
                self.assertEqual(finished,['7'] if command.startswith('/cancel') else [])
                self.assertEqual(len(notices),1)

    def test_edit_in_wrong_chat_preserves_session_and_does_not_publish(self):
        sent=[];finished=[];saved=[]
        namespace={'ALLOWED':{'7'},'PENDING_EDIT':SimpleNamespace(pop=lambda *a:1,finish=finished.append),
          'clean_operator_text':lambda text:(text,None),'send':lambda *args:None,
          '_mod':lambda mid:{'state':'carded','tg_chat_id':7,'tg_msg_id':200},
          '_set':lambda *args,**kwargs:saved.append(args),
          '_do_send':lambda *args,**kwargs:sent.append(args)}
        function('handle_message',namespace)({'from':{'id':7},'chat':{'id':99},'text':'Исправленный ответ'})
        self.assertEqual((sent,finished,saved),([],[],[]))

    def test_explicit_edit_sends_even_unchanged_draft_and_learns_only_after_success(self):
        for ok in (True,False):
            with self.subTest(ok=ok):
                sent=[];saved=[];learned=[];edits=[]
                row={'draft_text':'Ответ','is_answered':False,'posted_ok':False}
                namespace={'_mod':lambda mid:{'state':'edited','tg_chat_id':7,'tg_msg_id':200,'body':'Вопрос'},
                  '_fr':lambda mod:row,'_set':lambda *args,**kwargs:saved.append((args,kwargs)),
                  'runtime':SimpleNamespace(publication_block=lambda:None),
                  'publish_gate':SimpleNamespace(verdict=lambda *args:self.fail('Old draft gate must not reject operator text')),
                  'fs':SimpleNamespace(post_answer=lambda *args,**kwargs:(sent.append((args,kwargs)) or (ok,'published' if ok else 'unknown_outcome'))),
                  '_cache_remember':lambda *args,**kwargs:learned.append((args,kwargs)),
                  'edit_text':lambda *args,**kwargs:(edits.append(args) or {'ok':True}),
                  'html':__import__('html')}
                function('_do_send',namespace)(1,'7','Ответ',7,None,operator_edit=True)
                self.assertEqual(len(sent),1)
                self.assertEqual(sent[0][1],{'override':'правка оператора 7'})
                self.assertEqual(saved[0][0],(1,'sent' if ok else 'failed'))
                self.assertEqual(saved[0][1]['final_text'],'Ответ')
                self.assertEqual(len(learned),int(ok))
                if ok:self.assertFalse(learned[0][1]['check_gate'])
                self.assertEqual(edits[0][:2],(7,200))

    def test_expert_topic_for_other_series_does_not_suppress_search(self):
        notes=[{'brand':'hp','series_pattern':'W2030','topic':'прошивка','text':'exact scope'}]
        with patch.object(brand_notes,'all_notes',return_value=notes):
            self.assertFalse(brand_notes.covers('hp','Какая прошивка?','W2301X'))
            self.assertTrue(brand_notes.covers('hp','Какая прошивка?','W2030'))


if __name__=='__main__':unittest.main()
