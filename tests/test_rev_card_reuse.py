# поток: rev
"""Requeued moderation reuses its canonical Telegram message, including edit failures."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]

def load(path,name,namespace):
    tree=ast.parse((ROOT/path).read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]

class CardReuse(unittest.TestCase):
    def run_case(self,cycle,bound,edit_ok):
        row=dict(id=7,platform='wb',account='wb_acc1',kind='question',ext_id='q',
                 draft_text='Confirmed answer',draft_route='review',tg_chat_id=100 if bound else None,
                 tg_msg_id=22 if bound else None)
        calls=[]
        def send(*a,**kw):calls.append('send');return 33
        def edit(*a,**kw):calls.append('edit');return {'ok':True} if edit_ok else None
        bot=SimpleNamespace(_pending=lambda *a,**kw:[row],_kb=lambda *a,**kw:{},
              _card=lambda r:'card',send=send,edit_text=edit,NOTIFY_IDS=['100'],
              _set=lambda *a,**kw:calls.append(('saved',kw['tg_msg_id'])))
        if cycle:
            ns=dict(bot=bot,group=lambda r:'question',time=SimpleNamespace(sleep=lambda n:None))
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

if __name__=='__main__':unittest.main()
