# поток: rev
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from reports import simple_review_policy as policy, publish_gate, llm_batch, llm_client, batch_health
from collectors import feedback_autosend


class SimpleReviews(unittest.TestCase):
    def row(self, **kw):
        return dict(platform='wb', account='wb_acc1', kind='review', ext_id='test-simple',
                    rating=5, body='Отличный товар', pros='', cons='', **kw)

    def test_positive_and_negative_need_no_model_or_product_claims(self):
        row = self.row()
        with patch.object(llm_batch, 'ask', side_effect=AssertionError('Paid model called')):
            answer = policy.answer(row)
            negative = policy.answer(dict(row, rating=1, body='Брак, верните деньги'))
        self.assertEqual(answer[1], policy.POSITIVE)
        self.assertEqual(negative[1], policy.NEGATIVE)
        ready = dict(row, draft_text=answer[1], draft_grounding=answer[4], draft_route='auto')
        self.assertTrue(publish_gate.verdict(ready)[0])

    def test_question_and_positive_with_problem_are_not_auto_templates(self):
        row = self.row()
        self.assertIsNone(policy.answer(dict(row, kind='question')))
        self.assertIsNone(policy.answer(dict(row, body='Не подходит. Что купить?')))
        self.assertIsNone(policy.answer(dict(row, skipped_old=True)))
        self.assertIsNone(policy.answer(dict(row, platform='ozon')))

    def test_edited_or_forged_template_does_not_bypass_gate(self):
        row = self.row()
        row.update(draft_grounding=policy.answer(row)[4], draft_text='Подходит ко всем принтерам')
        self.assertFalse(policy.matches(row))
        self.assertIsNone(policy.answer(dict(row, final_text='Правка владельца')))
        self.assertIsNone(policy.answer(dict(row, editing=True)))

    def test_operator_started_editing_after_candidate_selection(self):
        row = dict(self.row(), draft_text=policy.POSITIVE)
        state = dict(final_text=None, decided_at=None, editing=True, state='carded')
        with patch.object(feedback_autosend.db, 'query', side_effect=[[{'id':1}], [state]]), \
             patch('feedback_bot.recovery_runtime.card_lock', return_value=MagicMock()), \
             patch.object(feedback_autosend.fs, 'post_answer') as send:
            self.assertEqual(feedback_autosend._post_candidate(row, 'fresh'),
                             (False, 'operator_decision_preserved'))
            send.assert_not_called()


class BatchFailures(unittest.TestCase):
    def flush_error(self, status):
        row = dict(req_key='test', model='claude-sonnet-4-6', system_text='system', content='public',
                   max_tokens=300, platform='wb', account='wb_acc1', kind='question', ext_id='test')
        error = RuntimeError('sensitive message must not be logged')
        error.status_code = status
        client = MagicMock()
        client.messages.batches.create.side_effect = error
        database = MagicMock()
        database.query.return_value = [row]
        with patch.object(llm_batch, 'db', database), patch.object(llm_batch, '_due', return_value=(True,1)), \
             patch.object(batch_health, 'blocked', return_value=None), \
             patch.object(batch_health, 'record', return_value='safe failure') as record, \
             patch.object(llm_client, 'client_for', return_value=client), \
             patch('reports.recovery_budget.setup'), \
             patch('reports.recovery_budget.status', return_value={'spent_usd':0,'reserved_usd':0}), \
             patch('reports.recovery_budget.reserve', return_value=True), \
             patch.dict('os.environ', {'FEEDBACK_LLM_PIPELINE_READY':'1'}):
            self.assertIsNone(llm_batch.flush_if_due(force=True, verbose=False))
            record.assert_called_once_with(error)
        return database

    def test_definitive_rejection_releases_reservation_and_unfreezes_queue(self):
        database = self.flush_error(400)
        cursor = database.get_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        sql = ' '.join(str(call.args[0]) for call in cursor.execute.call_args_list)
        self.assertIn('actual_usd=0', sql)
        self.assertIn('submission_key=NULL', sql)

    def test_unknown_timeout_does_not_release_reservation_or_enable_double_payment(self):
        database = self.flush_error(None)
        database.get_conn.assert_not_called()

    def test_billing_402_also_releases_unspent_reservation(self):
        database = self.flush_error(402)
        cursor = database.get_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.assertTrue(any('actual_usd=0' in str(call.args[0]) for call in cursor.execute.call_args_list))

    def test_workspace_limit_is_not_claimed_to_be_an_invalid_key(self):
        error = SimpleNamespace(status_code=400, body={'error':{'message':'workspace API usage limits'}})
        self.assertEqual(batch_health.classify(error)[0], 'workspace_limit')


if __name__ == '__main__':
    unittest.main()
