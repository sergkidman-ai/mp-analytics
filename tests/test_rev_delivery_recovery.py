# поток: rev
"""A source read cannot silently authorize a duplicate delivery."""
from contextlib import nullcontext
import datetime as dt
import unittest
from unittest.mock import patch

from reports import rev_delivery_recovery as recovery


def row(**changes):
    value=dict(platform='yandex',account='ya_acc1',kind='review',ext_id='187406026',
        state='blocked',ledger_state='rejected',attempts=1,attempt_blocked=False,
        detail='404 {"message":"Feedbacks not found."}',mod_id=17,
        approved_text=recovery.archive.POSITIVE,approval_basis=recovery.archive.APPROVAL,
        final_text=None,decided_at=None,moderation_state='archive_auto',editing=False)
    return dict(value,**changes)


def evidence(identifier='187406026',comments=None,**changes):
    value={'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        'feedback':{'http_status':200,'data':{'status':'OK','result':{'feedbacks':[{'feedbackId':int(identifier)}]}}},
        'comments':comments or [],'comments_complete':True,'comment_pages':[],
        'classification':'readable_but_write_rejected_404'}
    return dict(value,**changes)


class DeliveryRecoveryTests(unittest.TestCase):
    def test_unknown_stays_unknown_after_complete_empty_read(self):
        item=row(ext_id=recovery.KNOWN_TIMEOUT,ledger_state='unknown')
        self.assertEqual(recovery.classify(item,evidence(item['ext_id'])),'unknown_outcome_empty_readback')
        self.assertEqual(recovery.retry_allowed(item,evidence(item['ext_id']),0,150),
                         (False,'unknown_outcome_never_retry'))

    def test_customer_comment_cannot_confirm_own_delivery(self):
        item=row()
        data=evidence(comments=[{'canModify':False,'text':item['approved_text'],'status':'PUBLISHED'}])
        self.assertEqual(recovery.classify(item,data),'readable_but_write_rejected_404')

    def test_matching_own_comment_prevents_retry(self):
        item=row()
        data=evidence(comments=[{'canModify':True,'text':item['approved_text'],'status':'PUBLISHED'}])
        data['classification']=recovery.classify(item,data)
        self.assertEqual(data['classification'],'matching_own_answer')
        self.assertFalse(recovery.retry_allowed(item,data,0,150)[0])

    def test_incomplete_or_error_result_is_not_empty_readback(self):
        self.assertEqual(recovery.classify(row(),evidence(comments_complete=False)),'read_incomplete')
        data=evidence();data['feedback']['data']['status']='ERROR'
        self.assertEqual(recovery.classify(row(),data),'read_incomplete')

    def test_scope_is_exact(self):
        with self.assertRaises(ValueError):recovery.key(row(ext_id='other'))
        with self.assertRaises(ValueError):recovery.key(row(account='ya_acc2'))

    def test_only_one_approved_rejection_retry_with_fresh_evidence(self):
        self.assertTrue(recovery.retry_allowed(row(),evidence(),0,150)[0])
        for item in (row(attempts=2),row(attempt_blocked=True),row(final_text='Operator changed this'),
                     row(editing=True),row(decided_at='now'),row(approval_basis='not approved')):
            with self.subTest(item=item):self.assertFalse(recovery.retry_allowed(item,evidence(),0,150)[0])
        self.assertEqual(recovery.retry_allowed(row(),evidence(),556,150),(False,'daily_cap_reached'))
        old=evidence(at_utc=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=5)).isoformat())
        self.assertEqual(recovery.retry_allowed(row(),old,0,150),(False,'readback_expired'))

    def test_full_comment_pagination_and_repeated_cursor_fail_closed(self):
        calls=[]
        def read(path,body,params=None):
            calls.append(params)
            if path=='/goods-feedback':return evidence()['feedback']
            return {'http_status':200,'data':{'status':'OK','result':{
                'comments':[],'paging':{'nextPageToken':'same'}}}}
        result=recovery.inspect(row(),read)
        self.assertFalse(result['comments_complete'])
        self.assertEqual(result['classification'],'read_incomplete')
        self.assertEqual(calls[-1]['pageToken'],'same')
        self.assertEqual(len(calls),3)

    def test_cap_stops_before_any_source_or_publication_calls(self):
        with patch.object(recovery.runtime,'processing_lock',return_value=nullcontext()), \
             patch.object(recovery.runtime,'publication_block',return_value=False), \
             patch.object(recovery.sender,'_live',return_value=True), \
             patch.object(recovery.sender,'_live_accounts',return_value=['ya_acc1']), \
             patch.object(recovery.sender,'backlog_sent_today',return_value=556), \
             patch.object(recovery.sender,'_backlog_cap',return_value=150), \
             patch.object(recovery,'cases') as cases, \
             patch.object(recovery.sender,'send_yandex_review') as send:
            self.assertEqual(recovery.retry_known_rejections(),[
                {'result':'daily_cap_reached','used':556,'cap':150,'writes':0}])
            cases.assert_not_called();send.assert_not_called()

    def test_concurrent_operator_edit_and_ledger_change_cannot_send(self):
        for current,claim in ((row(final_text='Manual correction'),1),(row(),0)):
            with self.subTest(current=current,claim=claim), \
                 patch.object(recovery.runtime,'processing_lock',return_value=nullcontext()), \
                 patch.object(recovery.runtime,'card_lock',return_value=nullcontext()), \
                 patch.object(recovery.runtime,'publication_block',return_value=False), \
                 patch.object(recovery.sender,'_live',return_value=True), \
                 patch.object(recovery.sender,'_live_accounts',return_value=['ya_acc1']), \
                 patch.object(recovery.sender,'backlog_sent_today',return_value=0), \
                 patch.object(recovery.sender,'_backlog_cap',return_value=150), \
                 patch.object(recovery,'cases',side_effect=[[row()],[current]]), \
                 patch.object(recovery,'inspect',return_value=evidence()), \
                 patch.object(recovery,'save_evidence'), \
                 patch.object(recovery.db,'execute',return_value=claim), \
                 patch.object(recovery.sender,'send_yandex_review') as send:
                results=recovery.retry_known_rejections()
                send.assert_not_called()
                self.assertEqual(results[0]['result'],
                    'operator_decision_preserved' if current['final_text'] else 'ledger_changed')


if __name__=='__main__':unittest.main()
