# поток: rev
from contextlib import ExitStack,nullcontext
from unittest import TestCase,main
from unittest.mock import patch
from reports import recovery_archive_publish as archive

class ArchivePublication(TestCase):
    def row(self,**kwargs):
        row=dict(platform='yandex',account='ya_acc1',kind='review',ext_id='test',body='',pros='',cons='',
                 rating=5,mod_id=None,is_answered=False,posted_ok=False,
                 approved_text=archive.POSITIVE,approval_basis=archive.APPROVAL)
        return dict(row,**kwargs)

    def test_positive_and_negative_use_exact_approved_text(self):
        self.assertEqual(archive.reply_for(self.row()),('positive',archive.POSITIVE))
        self.assertEqual(archive.reply_for(self.row(rating=1)),('negative',archive.NEGATIVE))
        self.assertEqual(archive.reply_for(self.row(rating=5,body='Не подошёл к принтеру'))[0],'negative')
        self.assertNotIn('вернём',archive.NEGATIVE)
        self.assertNotIn('заменим',archive.NEGATIVE)

    def run_tick(self,row,send=(True,'sent'),read=None,extras=()):
        marks=[]
        with ExitStack() as stack:
            for name,value in [('setup',None),('refresh_cards',0),('withdraw_cards',0),('reconcile',0),('summary',[])]:
                stack.enter_context(patch.object(archive,name,return_value=value))
            stack.enter_context(patch.dict(archive.os.environ,{'FEEDBACK_YANDEX_ARCHIVE_AUTOREPLIES':'1'}))
            stack.enter_context(patch.object(archive.runtime,'publication_block',return_value=''))
            stack.enter_context(patch.object(archive.runtime,'card_lock',return_value=nullcontext()))
            stack.enter_context(patch.object(archive.runtime,'publication_result'))
            stack.enter_context(patch.object(archive.db,'query',side_effect=[[row],*extras]))
            stack.enter_context(patch.object(archive,'mark',side_effect=lambda *a:marks.append(a)))
            stack.enter_context(patch.object(archive,'update_card',return_value=True))
            stack.enter_context(patch.object(archive.sender,'backlog_sent_today',return_value=0))
            stack.enter_context(patch.object(archive.sender,'_backlog_cap',return_value=150))
            post=stack.enter_context(patch.object(archive.sender,'post_answer',return_value=send))
            stack.enter_context(patch.object(archive.source,'refresh',return_value=read or (True,row['approved_text'])))
            stack.enter_context(patch.object(archive.time,'sleep'))
            result=archive.tick()
            return result,marks,post.call_args_list

    def test_write_uses_existing_guarded_sender_and_confirms_readback(self):
        result,marks,calls=self.run_tick(self.row())
        self.assertEqual(result['accepted'],1);self.assertEqual(result['confirmed'],1)
        self.assertEqual(calls[0].kwargs,{'apply_cap':True,'override':archive.APPROVAL})
        self.assertEqual([m[1] for m in marks],['accepted','confirmed'])

    def test_existing_reply_is_not_sent_again(self):
        result,marks,calls=self.run_tick(self.row(is_answered=True))
        self.assertEqual(calls,[])
        self.assertEqual(marks[0][1],'observed_answer')

    def test_unknown_result_is_held_without_another_post(self):
        result,marks,calls=self.run_tick(self.row(),send=(False,'unknown response'),
            extras=([{'is_answered':False,'posted_ok':False}],[{'state':'unknown'}]))
        self.assertEqual(len(calls),1)
        self.assertEqual(result['unknown'],1)
        self.assertEqual(marks[-1][1],'unknown')

    def test_policy_never_targets_questions_or_other_marketplaces(self):
        for changes in [dict(kind='question'),dict(platform='wb'),dict(account='ya_acc2')]:
            with self.subTest(changes=changes),self.assertRaisesRegex(RuntimeError,'ArchiveScopeViolation'):
                self.run_tick(self.row(**changes))

    def test_unapproved_text_cannot_be_sent_with_override(self):
        with self.assertRaisesRegex(RuntimeError,'UnapprovedArchiveText'):
            self.run_tick(self.row(approved_text='Вернём деньги'))

    def test_manual_edits_and_skips_are_protected(self):
        for changes in [dict(final_text='Ручной текст'),dict(decided_at='now'),
                        dict(editing=True),dict(moderation_state='skipped')]:
            self.assertTrue(archive.owner_work(changes))

    def test_confirmed_post_retries_only_its_existing_card_update(self):
        row=dict(self.row(),mod_id=1,state='confirmed')
        with patch.object(archive.db,'query',return_value=[row]), \
             patch.object(archive.runtime,'card_lock',return_value=nullcontext()), \
             patch.object(archive,'update_card',return_value=True) as edit, \
             patch.object(archive.sender,'post_answer') as post:
            self.assertEqual(archive.refresh_cards(),1)
            edit.assert_called_once_with(row,'confirmed')
            post.assert_not_called()

if __name__=='__main__':main()
