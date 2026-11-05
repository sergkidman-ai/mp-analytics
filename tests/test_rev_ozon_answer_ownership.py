# поток: rev
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collectors import ozon_answer_owner as owner,feedback_preflight


class OzonOwnership(unittest.TestCase):
    def test_another_buyer_answer_does_not_close_seller_question(self):
        buyer={'author_name':'владимир к.','status_publication':'PUBLISHED','text':'buyer reply'}
        self.assertFalse(owner.is_own(buyer,{'Цифровой квадрат'}))
        self.assertTrue(owner.is_own(dict(buyer,author_name='Цифровой квадрат'),{'Цифровой квадрат'}))

    def test_identity_is_scoped_to_account_and_rejected_answer_is_not_published(self):
        self.assertFalse(owner.is_own({'author_name':'Dsquare','status_publication':'PUBLISHED'},{'Цифровой квадрат'}))
        self.assertFalse(owner.is_own({'author_name':'Dsquare','status_publication':'BANNED'},{'Dsquare'}))

    def test_unverified_seller_identity_blocks_role_guessing(self):
        with patch.object(owner.db,'query',return_value=[]):
            with self.assertRaisesRegex(RuntimeError,'OzonSellerIdentityUnverified'):
                owner.own_reply('test','q',1,read=lambda *a,**k:self.fail('No network without identity proof'))

    def test_answer_pages_find_our_answer_after_twenty_buyer_answers(self):
        buyers=[{'author_name':'buyer','status_publication':'PUBLISHED'} for _ in range(20)]
        seller={'author_name':'Dsquare','status_publication':'PUBLISHED','text':'our reply'}
        replies=[{'answers':buyers,'last_id':'next'},{'answers':[seller]}];calls=[]
        def read(*args,**kwargs):
            calls.append(kwargs['json']);return replies.pop(0)
        with patch.object(owner.db,'query',return_value=[{'author_name':'Dsquare'}]),patch.object(owner,'_headers',return_value={}):
            mine,all_answers=owner.own_reply('oz_acc2','q',1,read=read)
        self.assertEqual(mine,seller);self.assertEqual(len(all_answers),21)
        self.assertEqual(calls[1]['last_id'],'next')

    def test_live_preflight_allows_seller_reply_when_only_buyer_has_answered(self):
        row={'platform':'ozon','account':'oz_acc1','kind':'question','ext_id':'q','item_id':'1'}
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'id':'q','sku':1,'answers_count':1})
        with patch('collectors.ozon_feedbacks._headers',return_value={}),patch.object(feedback_preflight.requests,'post',return_value=response),patch.object(owner,'own_reply',return_value=(None,[{'author_name':'buyer'}])):
            self.assertEqual(feedback_preflight.probe(row),(False,''))

    def test_collector_keeps_new_question_open_despite_buyer_answer_count(self):
        path=Path(__file__).resolve().parents[1]/'collectors/feedback_recovery.py'
        tree=ast.parse(path.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='collect_ozon')
        saved=[]
        ns={'oz':SimpleNamespace(_headers=lambda account:{},_names=lambda account:{},QUESTION_URL='test'),
            'MAX_PAGES':1,'PAGE_SIZE':20,'Json':lambda value:value,'persist':lambda rows:saved.extend(rows),
            'read':lambda *a,**k:{'questions':[{'id':'q','sku':1,'status':'NEW','answers_count':1}]},
            'db':SimpleNamespace(execute=lambda *a:None),'time':SimpleNamespace(sleep=lambda n:None),
            'json':__import__('json'),'print':lambda *a,**k:None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
        with patch.object(owner,'own_reply',return_value=(None,[{'author_name':'buyer'}])):
            ns['collect_ozon']('oz_acc1')
        self.assertFalse(saved[0]['is_answered'])


if __name__=='__main__':unittest.main()
