# поток: rev
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from collectors import feedback_preflight as preflight


class PublicationReadback(unittest.TestCase):
    def test_previously_confirmed_but_missing_answer_is_flagged_without_resend(self):
        row={'platform':'yandex','account':'test','kind':'review','ext_id':'case'}
        with patch.object(preflight.db,'query',return_value=[row]),patch.object(preflight,'probe',return_value=(False,'')),patch.object(preflight,'mark_missing') as missing,patch('collectors.feedback_send.post_answer') as send:
            self.assertEqual(preflight.verify_recent_publications()['missing'],1)
            missing.assert_called_once_with(row);send.assert_not_called()

    def test_read_error_does_not_falsely_mark_publication_as_missing(self):
        row={'platform':'yandex','account':'test','kind':'review','ext_id':'case'}
        with patch.object(preflight.db,'query',return_value=[row]),patch.object(preflight.db,'execute'),patch.object(preflight,'probe',side_effect=TimeoutError),patch.object(preflight,'mark_missing') as missing:
            self.assertEqual(preflight.verify_recent_publications()['read_errors'],1)
            missing.assert_not_called()

    def test_confirmation_rechecks_are_bounded_and_rotated(self):
        row={'platform':'yandex','account':'test','kind':'review','ext_id':'case'}
        with patch.object(preflight.db,'query',return_value=[row]) as query, \
             patch.object(preflight.db,'execute') as update, \
             patch.object(preflight,'probe',return_value=(True,'Answer')):
            self.assertEqual(preflight.verify_recent_publications()['present'],1)
            self.assertIn("interval '6 hours'",query.call_args.args[0])
            self.assertEqual(query.call_args.args[1],(20,))
            update.assert_called_once()

    def test_wb_answer_pending_moderation_also_prevents_second_answer(self):
        row={'platform':'wb','account':'test','kind':'question','ext_id':'case'}
        response=SimpleNamespace(raise_for_status=lambda:None,
            json=lambda:{'error':False,'data':{'id':'case','answer':{'text':'Existing','state':'pending'}}})
        with patch('collectors.wb_feedbacks.feedback_token',return_value='test'),patch.object(preflight.requests,'get',return_value=response):
            self.assertEqual(preflight.probe(row),(True,'Existing'))

    def test_readback_of_another_id_is_rejected(self):
        row={'platform':'wb','account':'test','kind':'review','ext_id':'case'}
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'data':{'id':'other'}})
        with patch('collectors.wb_feedbacks.feedback_token',return_value='test'),patch.object(preflight.requests,'get',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'MismatchedWBReadback'):
                preflight.probe(row)

    def test_unknown_outcome_with_empty_readback_never_retries_publication(self):
        row={'attempted_text':'Answer'}
        with patch.object(preflight.db,'query',return_value=[row]),patch.object(preflight,'refresh',return_value=(False,'')),patch('collectors.feedback_send.post_answer') as send,patch('feedback_bot.recovery_runtime.publication_result') as result:
            self.assertEqual(preflight.reconcile_unknown()['still_unknown'],1)
            send.assert_not_called();result.assert_not_called()

    def test_unknown_outcome_with_matching_text_is_confirmed_without_resending(self):
        row={'attempted_text':'Answer'}
        with patch.object(preflight.db,'query',return_value=[row]),patch.object(preflight,'refresh',return_value=(True,'Answer')),patch('collectors.feedback_send.post_answer') as send,patch('collectors.feedback_send._mark_posted') as posted,patch('feedback_bot.recovery_runtime.publication_result') as result:
            self.assertEqual(preflight.reconcile_unknown()['confirmed'],1)
            send.assert_not_called();posted.assert_called_once_with(row,'Answer',True)
            result.assert_called_once_with(row,'confirmed')

    def test_answer_without_visible_text_closes_unknown_attempt_without_claiming_our_success(self):
        row={'attempted_text':'Answer'}
        with patch.object(preflight.db,'query',return_value=[row]),patch.object(preflight,'refresh',return_value=(True,'')),patch('collectors.feedback_send._mark_posted') as posted,patch('feedback_bot.recovery_runtime.publication_result') as result:
            self.assertEqual(preflight.reconcile_unknown()['observed_existing'],1)
            posted.assert_not_called();result.assert_called_once_with(row,'observed_answer')


if __name__=='__main__':unittest.main()
