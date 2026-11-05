# поток: rev
"""Requeued moderation reuses its canonical Telegram message, including edit failures."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from reports import publish_gate

ROOT=Path(__file__).resolve().parents[1]

def load(path,name,namespace):
    tree=ast.parse((ROOT/path).read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]

class CardReuse(unittest.TestCase):
    def run_case(self,cycle,bound,edit_ok,changes=None,markup_calls=None):
        row=dict(id=7,platform='wb',account='wb_acc1',kind='question',ext_id='q',
                 draft_text='Confirmed answer',draft_route='review',tg_chat_id=100 if bound else None,
                 tg_msg_id=22 if bound else None)
        row.update(changes or {})
        calls=[]
        def send(*a,**kw):calls.append('send');return 33
        def edit(*a,**kw):calls.append('edit');return {'ok':True} if edit_ok else None
        def keyboard(*args,**kwargs):
            if markup_calls is not None:markup_calls.append(kwargs)
            return {}
        bot=SimpleNamespace(_pending=lambda *a,**kw:[row],_kb=keyboard,
              _card=lambda r:'card',send=send,edit_text=edit,NOTIFY_IDS=['100'],
              _set=lambda *a,**kw:calls.append(('saved',kw['tg_msg_id'])))
        if cycle:
            ns=dict(bot=bot,publish_gate=publish_gate,group=lambda r:'question',time=SimpleNamespace(sleep=lambda n:None))
            load(Path('feedback_bot/recovery_cycle.py'),'send_cards',ns)()
        else:
            ns=dict(vars(bot),publish_gate=SimpleNamespace(verdict=lambda r:(True,''),can_override=lambda r:False),log=lambda s:None)
            load(Path('feedback_bot/tg_moderation.py'),'send_batch',ns)()
        return calls

    def test_both_senders_edit_existing_card_and_keep_binding(self):
        for cycle in [False,True]:
            with self.subTest(cycle=cycle):
                self.assertEqual(self.run_case(cycle,True,True),['edit',('saved',22)])

    def test_edit_failure_does_not_create_another_message(self):
        for cycle in [False,True]:
            with self.subTest(cycle=cycle):
                self.assertEqual(self.run_case(cycle,True,False),['edit'])

    def test_first_delivery_sends_and_saves_new_binding(self):
        for cycle in [False,True]:
            with self.subTest(cycle=cycle):
                self.assertEqual(self.run_case(cycle,False,True),['send',('saved',33)])

    def test_rejected_controller_draft_never_gets_green_send_button(self):
        markup=[]
        self.run_case(True,False,True,changes={'draft_route':'human','draft_grounding':{
            'recovery':True,'verify':'FIX'}},markup_calls=markup)
        self.assertEqual(markup,[{'allow_send':False,'allow_override':False}])

    def test_unconfirmed_fact_never_gets_green_send_button(self):
        markup=[]
        self.run_case(True,False,True,changes={'draft_grounding':{
            'recovery':True,'verify':'PASS','fact_block':'Нет подтверждения конкретной упаковки'}},markup_calls=markup)
        self.assertEqual(markup,[{'allow_send':False,'allow_override':False}])

    def test_empty_draft_cannot_be_approved_as_placeholder(self):
        markup=[]
        self.run_case(True,False,True,changes={'draft_text':''},markup_calls=markup)
        self.assertEqual(markup,[{'allow_send':False,'allow_override':False}])

    def test_confirmed_draft_has_send_button(self):
        markup=[]
        self.run_case(True,False,True,changes={'draft_grounding':{
            'recovery':True,'verify':'PASS','grounded':True}},markup_calls=markup)
        self.assertEqual(markup,[{'allow_send':True,'allow_override':False}])

    def test_blocked_card_banner_does_not_promise_green_button_send(self):
        ns={'runtime':SimpleNamespace(publication_block=lambda:''),
            'fs':SimpleNamespace(_live=lambda:True,_live_accounts=lambda:{'wb_acc1'}),
            '_published_today_line':lambda account:'count\n'}
        banner=load(Path('feedback_bot/tg_moderation.py'),'_mode_banner',ns)
        self.assertNotIn('по ✅',banner('wb_acc1',allow_send=False))
        self.assertIn('по ✅',banner('wb_acc1',allow_send=True))

if __name__=='__main__':unittest.main()
