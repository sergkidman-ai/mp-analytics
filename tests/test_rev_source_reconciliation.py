# поток: rev
import os
import unittest
from unittest.mock import patch
from collectors import feedback_recovery as source
from reports import deepseek_search, llm_batch


class SourceReconciliationTest(unittest.TestCase):
    def comments(self, items):
        with patch.object(source.db,'query',return_value=[]), \
             patch.object(source,'read',return_value={'result':{'comments':items}}), \
             patch.object(source.time,'sleep'):
            return source.yandex_review_answer('test-key','123','99')

    def test_another_business_comment_does_not_mean_we_answered(self):
        mine,comments=self.comments([{'canModify':False,'status':'PUBLISHED','author':{'type':'BUSINESS'},'text':'another seller'}])
        self.assertIsNone(mine)
        self.assertEqual(len(comments),1)

    def test_own_pending_answer_is_not_published_twice(self):
        item={'canModify':True,'status':'UNMODERATED','text':'our pending answer'}
        mine,_=self.comments([item])
        self.assertEqual(mine,item)

    def test_missing_search_key_cannot_spend_or_invent_evidence(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}), \
             patch.object(deepseek_search.requests,'post') as api:
            with self.assertRaises(RuntimeError):deepseek_search.facts('question','product')
            api.assert_not_called()

    def test_unvalidated_batch_does_not_submit_even_when_forced(self):
        with patch.dict(os.environ,{'FEEDBACK_LLM_PIPELINE_READY':'0'}), \
             patch.object(llm_batch,'_due') as due:
            self.assertIsNone(llm_batch.flush_if_due(force=True,verbose=False))
            due.assert_not_called()


if __name__=='__main__':unittest.main()
