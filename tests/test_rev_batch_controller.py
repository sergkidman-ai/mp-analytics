# поток: rev
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
import unittest
from reports import llm_routing, llm_batch


class BatchControllerTest(unittest.TestCase):
    def function(self, ask):
        path=Path(__file__).resolve().parents[1]/'reports/feedback_today.py'
        tree=ast.parse(path.read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_verified')
        namespace={'VERIFY_ON':True,'VERIFY_MODEL':'test-model','_brand_of':lambda r:'hp','json':json,'re':re,
                   'bn':SimpleNamespace(facts_for=lambda *a:('',0)),
                   'llm_batch':SimpleNamespace(ask=ask,LlmPending=llm_batch.LlmPending),
                   'llm_routing':llm_routing,'_COST':SimpleNamespace(add=lambda *a:None)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),namespace)
        return namespace['_verified']

    def test_controller_queues_batch_and_does_not_call_synchronous_client(self):
        calls=[]
        def ask(*args,**kwargs):
            calls.append(kwargs['purpose'])
            return '{"verdict":"PASS","note":"facts match"}'
        reply,ground=self.function(ask)(object(),{'kind':'question','body':'question'},'answer','facts',{})
        self.assertEqual(calls,['verify'])
        self.assertEqual(ground['verify'],'PASS')

    def test_pending_controller_remains_pending(self):
        def ask(*args,**kwargs):raise llm_batch.LlmPending('queued')
        with self.assertRaises(llm_batch.LlmPending):
            self.function(ask)(object(),{'kind':'question','body':'question'},'answer','facts',{})

    def test_controller_receives_selected_catalogue_listing_and_native_models(self):
        prompts=[]
        def ask(*args,**kwargs):
            prompts.append(args[2])
            return '{"verdict":"PASS","note":"catalogue proof supplied"}'
        selected={'ref':'Ozon SKU 4480823766','url':'https://www.ozon.ru/product/4480823766',
                  'native_card':{'code':'CLT-K406S','models':['SL-C460FW']}}
        _,ground=self.function(ask)(object(),{'kind':'question','body':'C460FW'},'Our SKU','original card',{'selected_offer':selected})
        self.assertEqual(ground['verify'],'PASS')
        self.assertIn('4480823766',prompts[0]);self.assertIn('SL-C460FW',prompts[0])

    def test_unparseable_controller_output_is_not_pass(self):
        message=SimpleNamespace(content=[SimpleNamespace(type='text',text='unstructured response')],usage=None)
        verdict,_,_=llm_routing.verify(lambda **kw:message,'test','question','question','answer','facts')
        self.assertEqual(verdict,'FIX')

    def test_truncated_controller_uses_one_separate_budgeted_retry(self):
        calls=[]
        def ask(*args,**kwargs):
            calls.append((kwargs['purpose'],args[3]))
            return '{"verdict":' if kwargs['purpose']=='verify' else '{"verdict":"PASS","note":"valid"}'
        _,ground=self.function(ask)(object(),{'kind':'question','body':'question'},'answer','facts',{})
        self.assertEqual(calls,[('verify',300),('verify_format_retry',800)])
        self.assertEqual(ground['verify'],'PASS')

    def test_controller_fact_rejection_does_not_retry(self):
        calls=[]
        def ask(*args,**kwargs):
            calls.append(kwargs['purpose'])
            return '{"verdict":"FIX","note":"missing evidence"}'
        _,ground=self.function(ask)(object(),{'kind':'question','body':'question'},'answer','facts',{})
        self.assertEqual(calls,['verify'])
        self.assertEqual(ground['verify'],'FIX')

    def test_review_controller_receives_all_fields_with_their_meaning(self):
        prompts=[]
        def ask(*args,**kwargs):
            prompts.append(args[2])
            return '{"verdict":"PASS","note":"whole review considered"}'
        row={'kind':'review','body':'','pros':'Отлично подходит, вопросов нет',
             'cons':'Нет, они отсутствовали'}
        _,ground=self.function(ask)(object(),row,'Спасибо за отзыв!','facts',{})
        self.assertEqual(ground['verify'],'PASS')
        self.assertIn('Достоинства: Отлично подходит',prompts[0])
        self.assertIn('Недостатки: Нет, они отсутствовали',prompts[0])

    def test_long_comment_does_not_hide_other_review_fields(self):
        prompts=[]
        def ask(*args,**kwargs):
            prompts.append(args[2])
            return '{"verdict":"PASS","note":"all fields supplied"}'
        row={'kind':'review','body':'a'*5000,'pros':'ДОСТОИНСТВО','cons':'НЕДОСТАТОК'}
        self.function(ask)(object(),row,'answer','facts',{})
        self.assertIn('Достоинства: ДОСТОИНСТВО',prompts[0])
        self.assertIn('Недостатки: НЕДОСТАТОК',prompts[0])


if __name__=='__main__':unittest.main()
